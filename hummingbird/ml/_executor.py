# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

import numpy as np
import torch

from hummingbird.ml._utils import pandas_installed, get_device, from_strings_to_ints
from hummingbird.ml.operator_converters import constants

if pandas_installed():
    from pandas import DataFrame
else:
    DataFrame = None


class Executor(torch.nn.Module, object):
    """
    Executor class able to run Hummingbird's internal representation of a converted pipeline.
    """

    def __init__(self, input_names, output_names, operator_map, operators, extra_config):
        """
        Args:
            input_names: The names of the input `onnxconverter_common.topology.Variable`s for this model
            output_names: The names of the output `onnxconverter_common.topology.Variable`s generated by this model
            operator_map: A dictionary of operator aliases and related PyTorch implementations
            operators: The list of operators (in a topological order) that will be executed by the model (in order)
            extra_config: Some additional custom configuration parameter
        """
        super(Executor, self).__init__()

        # Define input \ output names.
        # This is required because the internal variable names may differ from the original (raw) one.
        # This may happen, for instance, because we force our internal naming to be unique.
        def _fix_var_naming(operators, names, mod="input"):
            new_names = []
            map = {}

            for op in operators:
                if mod == "input":
                    iter = op.inputs
                else:
                    iter = op.outputs
                for i in iter:
                    for name in names:
                        if i.raw_name == name and name not in map:
                            map[i.raw_name] = i.full_name
                if len(map) == len(names):
                    break
            if map == {}:
                return names
            for name in names:
                new_names.append(map[name])
            return new_names

        self._input_names = _fix_var_naming(operators, input_names)
        self._output_names = _fix_var_naming(reversed(operators), output_names, "output")
        self._operators = torch.nn.ModuleList([operator_map[operator.full_name] for operator in operators])
        self.max_string_length = None

        if constants.MAX_STRING_LENGTH in extra_config:
            self.max_string_length = extra_config[constants.MAX_STRING_LENGTH]

    def forward(self, *inputs):
        print("-------------checkpoint-forward-------------------")
        print(*inputs)
        with torch.no_grad():
            assert len(self._input_names) == len(inputs) or (
                DataFrame is not None
                and isinstance(inputs[0], DataFrame)
                and not self.check_dataframe_to_array
                and len(self._input_names) == len(inputs[0].columns)
            ), "number of inputs or number of columns in the dataframe do not match with the expected number of inputs {}".format(
                self._input_names
            )

            if DataFrame is not None and isinstance(inputs[0], DataFrame):
                # Split the dataframe into column ndarrays.
                inputs = inputs[0]
                input_names = list(inputs.columns)
                splits = [inputs[input_names[idx]] for idx in range(len(input_names))]
                splits = [df.to_numpy().reshape(-1, 1) for df in splits]
                inputs = tuple(splits)
            inputs = [*inputs]
            variable_map = {}
            device = get_device(self)

            # Maps data inputs to the expected variables.
            for i, input_name in enumerate(self._input_names):
                input_ = inputs[i]
                if type(input_) is list:
                    input_ = np.array(input_)
                if type(input_) is np.ndarray:
                    # Convert string arrays into int32.
                    if input_.dtype.kind in constants.SUPPORTED_STRING_TYPES:
                        assert self.max_string_length is not None

                        input_ = from_strings_to_ints(input_, self.max_string_length)
                    elif input_.dtype.kind == "M":  # Datetime
                        # We convert into seconds from 1970-1-1.
                        input_ = (input_ - np.datetime64("1970-01-01T00:00:00.000000000")).astype(np.int64) / 1000000000
                    input_ = torch.from_numpy(input_)
                elif type(input_) is not torch.Tensor:
                    raise RuntimeError("Inputer tensor {} of not supported type {}".format(input_name, type(input_)))
                if input_.dtype == torch.float64:
                    # We convert double precision arrays into single precision. Sklearn does the same.
                    input_ = input_.float()
                if device is not None and device.type != "cpu":
                    input_ = input_.to(device)
                variable_map[input_name] = input_

            # Evaluate all the operators in the topology by properly wiring inputs \ outputs
            for operator in self._operators:
                outputs = operator(*(variable_map[input_name] for input_name in operator.inputs))

                if len(operator.outputs) == 1:
                    variable_map[operator.outputs[0]] = outputs
                else:
                    for i, output_name in enumerate(operator.outputs):
                        variable_map[output_name] = outputs[i]
            print("self._output_names",self._output_names)
            # Prepare and return the output.
            if len(self._output_names) == 1:
                return variable_map[self._output_names[0]]
            else:
                return tuple(variable_map[output_name] for output_name in self._output_names)
