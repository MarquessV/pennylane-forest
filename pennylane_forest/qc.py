"""
Base Quantum Computer device class
==================================

**Module name:** :mod:`pennylane_forest.qc`

.. currentmodule:: pennylane_forest.qc

This module contains a base class for constructing pyQuil Quantum Computer devices for PennyLane.

Auxiliary functions
-------------------

.. autosummary::
    basis_state
    rotation
    controlled_phase

Classes
-------

.. autosummary::
   ForestDevice

Code details
~~~~~~~~~~~~
"""

from abc import ABC, abstractmethod
from typing import Dict

import numpy as np

from collections import OrderedDict

from pyquil import Program
from pyquil.api import QAMExecutionResult, QuantumComputer
from pyquil.gates import RESET, MEASURE
from pyquil.quil import Pragma

from pennylane import DeviceError
from pennylane.wires import Wires

from .device import ForestDevice
from ._version import __version__


class QuantumComputerDevice(ForestDevice, ABC):
    r"""Abstract Quantum Computer device for PennyLane.

    This is a base class for common logic shared by pyQuil ``QuantumComputer``s (i.e. QVMs and real QPUs).
    Children classes at minimum need to define a `get_qc` method that returns a pyQuil ``QuantumComputer``.

    Args:
        device (str): the name of the device to initialise.
        shots (int): number of circuit evaluations/random samples used
            to estimate expectation values of observables.
         wires (Iterable[Number, str]): Iterable that contains unique labels for the
            qubits as numbers or strings (i.e., ``['q1', ..., 'qN']``).
            The number of labels must match the number of qubits accessible on the backend.
            If not provided, qubits are addressed as consecutive integers ``[0, 1, ...]``, and their number
            is inferred from the backend.
        active_reset (bool): whether to actively reset qubits instead of waiting for
            for qubits to decay to the ground state naturally.
            Setting this to ``True`` results in a significantly faster expectation value
            evaluation when the number of shots is larger than ~1000.

    Keyword args:
        compiler_timeout (int): number of seconds to wait for a response from quilc (default 10).
        execution_timeout (int): number of seconds to wait for a response from the QVM (default 10).
        parametric_compilation (bool): a boolean value of whether or not to use parametric
            compilation.
    """
    pennylane_requires = ">=0.17"
    version = __version__
    author = "Rigetti Computing Inc."

    def __init__(
        self, device, *, shots=1000, wires=None, active_reset=False, **kwargs
    ):
        if shots is not None and shots <= 0:
            raise ValueError("Number of shots must be a positive integer or None.")

        self._compiled_program = None

        self.parametric_compilation = kwargs.get("parametric_compilation", True)

        if self.parametric_compilation:
            self._circuit_hash = None
            """None or int: stores the hash of the circuit from the last execution which
            can be used for parametric compilation."""

            self._compiled_program_dict = {}
            """dict[int, pyquil.ExecutableDesignator]: stores circuit hashes associated
                with the corresponding compiled programs."""

            self._parameter_map = {}
            """dict[str, float]: stores the string of symbolic parameters associated with
                their numeric values. This map will be used to bind parameters in a parametric
                program using PyQuil."""

            self._parameter_reference_map = {}
            """dict[str, pyquil.quilatom.MemoryReference]: stores the string of symbolic
                parameters associated with their PyQuil memory references."""

        timeout_args = self._get_timeout_args(**kwargs)

        self.qc = self.get_qc(device, **timeout_args)

        self.num_wires = len(self.qc.qubits())

        if wires is None:
            # infer the number of modes from the device specs
            # and use consecutive integer wire labels
            wires = range(self.num_wires)

        if isinstance(wires, int):
            raise ValueError(
                "Device has a fixed number of {} qubits. The wires argument can only be used "
                "to specify an iterable of wire labels.".format(self.num_wires)
            )

        if self.num_wires != len(wires):
            raise ValueError(
                "Device has a fixed number of {} qubits and "
                "cannot be created with {} wires.".format(self.num_wires, len(wires))
            )

        self.wiring = {i: q for i, q in enumerate(self.qc.qubits())}
        self.active_reset = active_reset

        super().__init__(wires, shots)
        self.reset()

    @abstractmethod
    def get_qc(self, device, *, noisy, **kwargs) -> QuantumComputer:
        """Initializes and returns a pyQuil QuantumComputer that can run quantum programs"""
        pass

    @property
    def circuit_hash(self):
        if self.parametric_compilation:
            return self._circuit_hash

        return None

    @property
    def compiled_program(self):
        """Returns the latest program that was compiled for running.

        If parametric compilation is turned on, this will be a parametric program.

        The program is returned as a string of the Quil code.
        If no program was compiled yet, this property returns None.

        Returns:
            Union[None, str]: the latest compiled program
        """
        return str(self._compiled_program) if self._compiled_program else None

    @property
    def operations(self):
        return set(self._operation_map.keys())

    @property
    def program(self):
        """View the last evaluated Quil program"""
        return self.prog

    def _get_timeout_args(self, **kwargs) -> Dict[str, float]:
        timeout_args = {}
        if "compiler_timeout" in kwargs:
            timeout_args["compiler_timeout"] = kwargs["compiler_timeout"]

        if "execution_timeout" in kwargs:
            timeout_args["execution_timeout"] = kwargs["execution_timeout"]

        return timeout_args

    def define_wire_map(self, wires):
        if hasattr(self, "wiring"):
            device_wires = Wires(self.wiring)
        else:
            # if no wiring given, use consecutive wire labels
            device_wires = Wires(range(self.num_wires))

        return OrderedDict(zip(wires, device_wires))

    def apply(self, operations, **kwargs):
        prag = Program(Pragma("INITIAL_REWIRING", ['"PARTIAL"']))
        if self.active_reset:
            prag += RESET()
        self.prog = prag + self.prog

        if self.parametric_compilation and "pyqvm" not in self.qc.name:
            self.prog += self.apply_parametric_operations(operations)
        else:
            self.prog += self.apply_circuit_operations(operations)

        rotations = kwargs.get("rotations", [])
        self.prog += self.apply_rotations(rotations)

        qubits = sorted(self.wiring.values())
        ro = self.prog.declare("ro", "BIT", len(qubits))
        for i, q in enumerate(qubits):
            self.prog.inst(MEASURE(q, ro[i]))

        self.prog.wrap_in_numshots_loop(self.shots)

    def apply_parametric_operations(self, operations):
        """Applies a parametric program by applying parametric operation with symbolic parameters.

        Args:
            operations (List[pennylane.Operation]): quantum operations that need to be applied

        Returns:
            pyquil.Prgram(): a pyQuil Program with the given operations
        """
        prog = Program()
        # Apply the circuit operations
        for i, operation in enumerate(operations):
            # map the operation wires to the physical device qubits
            device_wires = self.map_wires(operation.wires)

            if i > 0 and operation.name in ("QubitStateVector", "BasisState"):
                raise DeviceError(
                    "Operation {} cannot be used after other Operations have already been applied "
                    "on a {} device.".format(operation.name, self.short_name)
                )

            # Prepare for parametric compilation
            par = []
            for param in operation.data:
                if getattr(param, "requires_grad", False) and operation.name != "BasisState":
                    # Using the idx for trainable parameter objects to specify the
                    # corresponding symbolic parameter
                    parameter_string = "theta" + str(id(param))

                    if parameter_string not in self._parameter_reference_map:
                        # Create a new PyQuil memory reference and store it in the
                        # parameter reference map if it was not done so already
                        current_ref = self.prog.declare(parameter_string, "REAL")
                        self._parameter_reference_map[parameter_string] = current_ref

                    # Store the numeric value bound to the symbolic parameter
                    self._parameter_map[parameter_string] = [param.unwrap()]

                    # Appending the parameter reference to the parameters
                    # of the corresponding operation
                    par.append(self._parameter_reference_map[parameter_string])
                else:
                    par.append(param)

            prog += self._operation_map[operation.name](*par, *device_wires.labels)

        return prog

    def execute(self, circuit, **kwargs):
        if self.parametric_compilation:
            self._circuit_hash = circuit.graph.hash
        return super().execute(circuit, **kwargs)

    def generate_samples(self):
        if self.parametric_compilation:
            for region, value in self._parameter_map.items():
                self.prog.write_memory(region_name=region, value=value)

        if "pyqvm" in self.qc.name:
            return self.extract_samples(self.qc.run(self.prog))

        if self.circuit_hash is None:
            # Parametric compilation was set to False
            # Compile the program
            self._compiled_program = self.qc.compile(self.prog)
            results = self.qc.run(executable=self._compiled_program)
            return self.extract_samples(results)

        if self.circuit_hash not in self._compiled_program_dict:
            # Compiling this specific program for the first time
            # Store the compiled program with the corresponding hash
            self._compiled_program_dict[self.circuit_hash] = self.qc.compile(self.prog)

        # The program has been compiled, store as the latest compiled program
        self._compiled_program = self._compiled_program_dict[self.circuit_hash]
        results = self.qc.run(executable=self._compiled_program)
        return self.extract_samples(results)

    def extract_samples(self, execution_results: QAMExecutionResult) -> np.ndarray:
        """Returns samples from the readout register on the execution results received after
        running a program on a pyQuil Quantum Abstract Machine.

        Returns:
            numpy.ndarray: Samples extracted from the readout register on the execution results.
        """
        return execution_results.readout_data.get("ro", [])

    def reset(self):
        """Resets the device after the previous run.

        Note:
            The ``_compiled_program`` and the ``_compiled_program_dict`` attributes are
            not reset such that these can be used upon multiple device execution.
        """
        super().reset()

        if self.parametric_compilation:
            self._circuit_hash = None
            self._parameter_map = {}
            self._parameter_reference_map = {}

    def mat_vec_product(self, mat, vec, device_wire_labels):
        r"""Apply multiplication of a matrix to subsystems of the quantum state.

        Args:
            mat (array): matrix to multiply
            vec (array): state vector to multiply
            device_wire_labels (Sequence[int]): labels of device subsystems

        Returns:
            array: output vector after applying ``mat`` to input ``vec`` on specified subsystems
        """
        num_wires = len(device_wire_labels)

        if mat.shape != (2**num_wires, 2**num_wires):
            raise ValueError(
                f"Please specify a {2 ** num_wires} x {2 ** num_wires} matrix for {num_wires} wires."
            )

        # first, we need to reshape both the matrix and vector
        # into blocks of 2x2 matrices, in order to do the higher
        # order matrix multiplication

        # Reshape the matrix to ``size=[2, 2, 2, ..., 2]``,
        # where ``len(size) == 2*len(wires)``
        #
        # The first half of the dimensions correspond to a
        # 'ket' acting on each wire/qubit, while the second
        # half of the dimensions correspond to a 'bra' acting
        # on each wire/qubit.
        #
        # E.g., if mat = \sum_{ijkl} (c_{ijkl} |ij><kl|),
        # and wires=[0, 1], then
        # the reshaped dimensions of mat are such that
        # mat[i, j, k, l] == c_{ijkl}.
        mat = np.reshape(mat, [2] * len(device_wire_labels) * 2)

        # Reshape the state vector to ``size=[2, 2, ..., 2]``,
        # where ``len(size) == num_wires``.
        # Each wire corresponds to a subsystem.
        #
        # E.g., if vec = \sum_{ijk}c_{ijk}|ijk>,
        # the reshaped dimensions of vec are such that
        # vec[i, j, k] == c_{ijk}.
        vec = np.reshape(vec, [2] * self.num_wires)

        # Calculate the axes on which the matrix multiplication
        # takes place. For the state vector, this simply
        # corresponds to the requested wires. For the matrix,
        # it is the latter half of the dimensions (the 'bra' dimensions).
        #
        # For example, if num_wires=3 and wires=[2, 0], then
        # axes=((2, 3), (2, 0)). This is equivalent to doing
        # np.einsum("ijkl,lnk", mat, vec).
        axes = (np.arange(len(device_wire_labels), 2 * len(device_wire_labels)), device_wire_labels)

        # After the tensor dot operation, the resulting array
        # will have shape ``size=[2, 2, ..., 2]``,
        # where ``len(size) == num_wires``, corresponding
        # to a valid state of the system.
        tdot = np.tensordot(mat, vec, axes=axes)

        # Tensordot causes the axes given in `wires` to end up in the first positions
        # of the resulting tensor. This corresponds to a (partial) transpose of
        # the correct output state
        # We'll need to invert this permutation to put the indices in the correct place
        unused_idxs = [idx for idx in range(self.num_wires) if idx not in device_wire_labels]
        perm = device_wire_labels + unused_idxs

        # argsort gives the inverse permutation
        inv_perm = np.argsort(perm)
        state_multi_index = np.transpose(tdot, inv_perm)

        return np.reshape(state_multi_index, 2**self.num_wires)

    def analytic_probability(self, wires=None):
        """Return the (marginal) probability of each computational basis
        state from the last run of the device.

        If no wires are specified, then all the basis states representable by
        the device are considered and no marginalization takes place.

        .. warning:: This method will have to be redefined for hardware devices, since it uses
            the ``device._state`` attribute. This attribute might not be available for such devices.

        Args:
            wires (Iterable[Number, str], Number, str, Wires): wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            List[float]: list of the probabilities
        """
        if self._state is None:
            return None

        prob = self.marginal_prob(np.abs(self._state) ** 2, wires)
        return prob
