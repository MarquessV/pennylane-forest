"""
QVM Device
==========

**Module name:** :mod:`pennylane_forest.qvm`

.. currentmodule:: pennylane_forest.qvm

This module contains the :class:`~.QVMDevice` class, a PennyLane device that allows
evaluation and differentiation of Rigetti's Forest Quantum Virtual Machines (QVMs)
using PennyLane.

Classes
-------

.. autosummary::
   QVMDevice

Code details
~~~~~~~~~~~~
"""
from typing import Dict

import networkx as nx
from pyquil import get_qc
from pyquil.api import QAMExecutionResult, QuantumComputer
from pyquil.api._quantum_computer import _get_qvm_with_topology
from pyquil.gates import MEASURE, RESET
from pyquil.quil import Pragma, Program

from pennylane import DeviceError, numpy as np

from .device import ForestDevice


class QVMDevice(ForestDevice):
    r"""Forest QVM device for PennyLane.

    This device supports both the Rigetti Lisp QVM, as well as the built-in pyQuil pyQVM.
    If using the pyQVM, the ``qvm_url`` QVM server url keyword argument does not need to
    be set.

    Args:
        device (Union[str, nx.Graph]): the name or topology of the device to initialise.

            * ``Nq-qvm``: for a fully connected/unrestricted N-qubit QVM
            * ``9q-square-qvm``: a :math:`9\times 9` lattice.
            * ``Nq-pyqvm`` or ``9q-square-pyqvm``, for the same as the above but run
              via the built-in pyQuil pyQVM device.
            * Any other supported Rigetti device architecture.
            * Graph topology representing the device architecture.

        shots (None, int, list[int]): Number of circuit evaluations/random samples used to estimate
            expectation values of observables. If ``None``, the device calculates probability, expectation values,
            and variances analytically. If an integer, it specifies the number of samples to estimate these quantities.
            If a list of integers is passed, the circuit evaluations are batched over the list of shots.
        wires (Iterable[Number, str]): Iterable that contains unique labels for the
            qubits as numbers or strings (i.e., ``['q1', ..., 'qN']``).
            The number of labels must match the number of qubits accessible on the backend.
            If not provided, qubits are addressed as consecutive integers [0, 1, ...], and their number
            is inferred from the backend.
        noisy (bool): set to ``True`` to add noise models to your QVM.

    Keyword args:
        compiler_timeout (int): number of seconds to wait for a response from quilc (default 10).
        execution_timeout (int): number of seconds to wait for a response from the QVM (default 10).
        parametric_compilation (bool): a boolean value of whether or not to use parametric
            compilation.
    """
    name = "Forest QVM Device"
    short_name = "forest.qvm"
    observables = {"PauliX", "PauliY", "PauliZ", "Identity", "Hadamard", "Hermitian"}

    def __init__(self, device, *, wires=None, shots=1000, noisy=False, **kwargs):
        if shots is None:
            raise ValueError("QVM device cannot be used for analytic computations.")

        super().__init__(device, wires=wires, shots=shots, noisy=noisy, active_reset=False, **kwargs)

    def get_qc(self, device, noisy, **kwargs) -> QuantumComputer:
        if isinstance(device, nx.Graph):
            client_configuration = super()._get_client_configuration()
            return _get_qvm_with_topology(
                name="device",
                topology=device,
                noisy=noisy,
                client_configuration=client_configuration,
                qvm_type="qvm",
                compiler_timeout=kwargs.get(
                    "compiler_timeout", 10.0
                ),  # 10.0 is the pyQuil default
                execution_timeout=kwargs.get(
                    "execution_timeout", 10.0
                ),  # 10.0 is the pyQuil default
            )
        elif isinstance(device, str):
            return get_qc(device, as_qvm=True, noisy=noisy, **kwargs)

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
