import os
import os
import re
from abc import abstractmethod, ABC, ABCMeta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Tuple, List, Dict, Callable

import numpy as np
import pyastyle
from mako import exceptions
from mako.template import Template
from pyopencl import Program, create_some_context
from pyopencl._cl import CommandQueue, Kernel, Context, get_platforms, device_type, Device, command_queue_properties
from pyopencl.array import Array, to_device

from pyopencl_extension import np_cl_types
from pyopencl_extension.helpers import write_string_to_file
from pyopencl_extension.np_cl_types import number_vec_elements_of_cl_type, c_name_from_dtype, scalar_type_from_vec_type, \
    get_vec_size, ClTypes
from pyopencl_extension.unparse import create_py_file_and_load_module, unparse_c_code_to_python

__author__ = "piveloper"
__copyright__ = "26.03.2020, piveloper"
__version__ = "1.0"
__email__ = "piveloper@gmail.com"
__doc__ = """This script includes helpful functions to extended PyOpenCl functionality."""

preamble_activate_double = """
#if defined(cl_khr_fp64)  // Khronos extension available?
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#define PYOPENCL_DEFINE_CDOUBLE
#elif defined(cl_amd_fp64)  // AMD extension available?
#pragma OPENCL EXTENSION cl_amd_fp64 : enable
#define PYOPENCL_DEFINE_CDOUBLE
#endif
"""

preamble_activate_complex_numbers = """
#include <pyopencl-complex.h>
#define TP_ROOT ${cplx_type}
"""


def preamble_precision(precision: str = 'single'):
    """
    This function generates preamble to support either single or double precision floating point numbers.
    :param precision:
    :return:
    """
    if precision == 'single':
        return """
#define PI 3.14159265359f
            """
    elif precision == 'double':
        return preamble_activate_double + """\n
#define PI 3.14159265358979323846
            """
    else:
        raise NotImplementedError()


def preamble_generic_type_operations(number_format: str = 'real', precision: str = 'single'):
    """
    This function returns a preamble which defines how generic operations are executed on device.

    This becomes especially important when dealing with complex types which  OpenCl does not support out of the box.
    As a solution, pyopencl-complex.h includes several basic function for complex operations.

    E.g. consider a kernel which adds two numbers, however the input type can be real or complex valued.
    Typically one would implement c = a + b. However, OpenCl does not support + operation when a and b are complex
    valued. Therefore using this preamble one can write c = ADD(a,b). ADD acts a a generic operation which supports
    real and complex input depending on selection for number_format.


    :param number_format: 'real' or 'complex
    :param precision: 'single' or 'double'
    :return: preamble to support generic operations
    """

    if number_format == 'complex':
        cplx_type_pyopencl = {'single': 'cfloat',
                              'double': 'cdouble'}[precision]
        return preamble_activate_complex_numbers + """
#define MUL ${cplx_type}_mul
#define ADD ${cplx_type}_add
#define SUB ${cplx_type}_sub
#define ABS ${cplx_type}_abs
#define RMUL ${cplx_type}_rmul
#define NEW ${cplx_type}_new
#define CONJ ${cplx_type}_conj
#define REAL(x) x.real
#define IMAG(x) x.imag
    """.replace('${cplx_type}', cplx_type_pyopencl)
    elif number_format == 'real':
        return """
#define MUL(x,y) (x*y)
#define ADD(x,y) (x+y)
#define SUB(x,y) (x-y)
#define ABS(x) (fabs(x))
#define RMUL(x,y) (x*y)
#define NEW(x,y) (x)
#define CONJ(x) (x)
#define REAL(x) (x)
#define IMAG(x) (0)
        """
    else:
        raise NotImplementedError()


@dataclass
class QueueProperties:
    DEFAULT: int = 0
    ON_DEVICE: int = 4
    ON_DEVICE_DEFAULT: int = 8
    OUT_OF_ORDER_EXEC_MODE_ENABLE: int = 1
    PROFILING_ENABLE: int = 2


class CommandQueueExtended(CommandQueue):
    def __init__(self, context, device=None, properties=0):
        super().__init__(context, device, properties)
        self.events = []


class Profiling:
    def __init__(self, queue: CommandQueueExtended):
        if queue.properties == command_queue_properties.PROFILING_ENABLE:
            self.queue = queue
            self.events = queue.events
        else:
            raise ValueError('Profiling must be enabled using command_queue_properties.PROFILING_ENABLE')

    def get_knl_names_time_ms(self):
        return [(event[0], 1e-6 * ((_ := event[1]).profile.end - _.profile.start)) for event in self.events]

    def list_cumulative_knl_times_ms(self):
        knl_names = {_[0]: 0.0 for _ in self.get_knl_names_time_ms()}
        for _ in self.get_knl_names_time_ms():
            knl_names[_[0]] += _[1]
        indices = np.argsort([knl_names[_] for _ in knl_names])
        return [list(knl_names.items())[i] for i in np.flip(indices)]

    def get_sum_execution_time(self):
        return sum([_[1] for _ in self.list_cumulative_knl_times_ms()])

    def show_histogram_cumulative_kernel_times(self):
        import matplotlib.pyplot as plt
        import numpy as np
        fig, ax = plt.subplots()
        fig.subplots_adjust(left=0.4)
        # Example data
        knl_names = [_[0] for _ in self.list_cumulative_knl_times_ms()]
        knl_times = [_[1] for _ in self.list_cumulative_knl_times_ms()]
        y_pos = np.arange(len(knl_names))
        ax.barh(y_pos, knl_times, align='center')
        ax.set_yticks(y_pos)
        ax.set_yticklabels(knl_names)
        ax.invert_yaxis()  # labels read top-to-bottom
        ax.set_xlabel('Time ms')
        ax.set_title(f'Sum execution time {self.get_sum_execution_time()}')
        plt.show()


@dataclass
class ClInit:
    context: Context = None
    queue: CommandQueueExtended = None
    b_compiler_output: bool = True
    # https://stackoverflow.com/questions/29068229/is-there-a-way-to-profile-an-opencl-or-a-pyopencl-program
    b_profiling_enable: bool = False
    queue_properties: int = QueueProperties.DEFAULT

    @property
    def device(self) -> Device:
        return self.context.devices[0]

    @staticmethod
    def from_buffer(buffer: Array) -> 'ClInit':
        return ClInit(queue=buffer.queue, context=buffer.context)

    def __post_init__(self):
        if self.context is None:
            self.context = create_some_context()
        if self.queue is None:
            if self.b_profiling_enable:
                self.queue = CommandQueueExtended(self.context, properties=QueueProperties.PROFILING_ENABLE)
            else:
                self.queue = CommandQueueExtended(self.context, properties=self.queue_properties)
        self.queue.finish()
        if self.b_compiler_output:
            os.environ['PYOPENCL_COMPILER_OUTPUT'] = '1'
        else:
            os.environ['PYOPENCL_COMPILER_OUTPUT'] = '0'


def get_device(device: Tuple[int, int]) -> Device:
    platform = get_platforms()
    my_gpu_devices = [platform[device[0]].get_devices(device_type=device_type.GPU)[device[1]]][0]
    return my_gpu_devices


def get_cl_init(device: Tuple[int, int], b_profiling_enable=False) -> ClInit:
    """
    On a computer often multiple chips exist to execute OpenCl code, like Intel, AMD or Nvidia GPUs or FPGAs.

    This function facilitates to get a context and queue pointing to a particular device.

    The first parameter device[0] selects the platform, like Intel or AMD.

    The second parameter selects a particular device of that platform, e.g. if multiple AMD Graphiccards ar installed.

    :param device: e.g. deice= (0,1). 0 refers to the platform, and 1 to the device index on that platform.
    :return: A container class with context and queue pointing to selected device.
    """
    if device[0] == 'some_context':
        context = create_some_context()
    else:
        platform = get_platforms()
        try:
            my_gpu_devices = [platform[device[0]].get_devices(device_type=device_type.GPU)[device[1]]]
        except:
            try:
                my_gpu_devices = platform[device[0]].get_devices()
            except:
                raise ValueError('No matching device found')
        context = Context(devices=my_gpu_devices)
    return ClInit(context, b_profiling_enable=b_profiling_enable)


def catch_invalid_argument_name(name: str):
    """
    E.g. when using certain argument names like 'channel' the opencl compiler throws a compilation error, probably
    because channel is an reserved opencl command. There we replace those names by appending '_' character.

    :param name:
    :return:
    """
    invalid_names = ['channel']
    if name in invalid_names:
        raise ValueError('Invalid opencl name: \'{}\' used.'.format(name))
    else:
        return name


@dataclass
class ArgBase(ABC):
    # too much restriction, shape of array might change during runtime
    # shape: Tuple[int, ...] = (1,)  # default: argument is scalar

    @property
    @abstractmethod
    def address_space_qualifier(self) -> str:
        # __global, __local, __private, __constant
        pass

    @property
    @abstractmethod
    def dtype(self) -> np.dtype:
        # __global, __local, __private, __constant
        pass

    def to_string(self, name):
        new_name = catch_invalid_argument_name(name)
        if type(self) in [ArgScalar, KnlArgScalar]:  # scalar
            return '{} {} {}'.format(self.address_space_qualifier, c_name_from_dtype(self.dtype), new_name)
        else:  # array
            return '{} {} *{}'.format(self.address_space_qualifier, c_name_from_dtype(self.dtype), new_name)


@dataclass
class ArgScalar(ArgBase):
    dtype: np.dtype = field(default=ClTypes.int)
    b_scalar: bool = field(init=False, default=True)
    address_space_qualifier: str = field(default='')


@dataclass
class ArgPrivate(ArgBase):
    dtype: np.dtype = field(default=ClTypes.int)
    address_space_qualifier: str = field(init=False, default='__private')


@dataclass
class ArgLocal(ArgBase):
    dtype: np.dtype = field(default=ClTypes.int)
    address_space_qualifier: str = field(init=False, default='__local')


@dataclass
class ArgConstant(ArgBase):
    """
    const is only a hint for the compiler that the data does not change
    __constant leads to usage of very fast constant cache memory which is shared among
    multiple compute units. From AMD optimization guide, e.g. we can read 4 bytes/cycles.
    Local memory can be ready twice as fast with 8bytes/cycle, however local memory is a even more scarce resource.
    """
    dtype: np.dtype = field(default=ClTypes.int)
    address_space_qualifier: str = field(init=False, default='__constant')


@dataclass
class ArgBuffer(ArgBase):
    dtype: np.dtype = field(default=ClTypes.int)
    address_space_qualifier: str = field(default='')

    def __post_init__(self):
        if self.address_space_qualifier != '__constant':
            self.address_space_qualifier = '__global {}'.format(self.address_space_qualifier)


# @dataclass
# class ArgGlobal(ArgBuffer):  # todo, rename all usages of ArgBuffer to
#     pass


ScalarArgTypes = Union[str, float, int, bool]
KnlArgTypes = Union[Array, ScalarArgTypes]


@dataclass
class KnlArgScalar(ArgBase):
    dtype: np.dtype = None
    default: ScalarArgTypes = None
    address_space_qualifier: str = field(init=False, default='')

    def __post_init__(self):
        if self.dtype is None:
            if self.default is None:
                raise ValueError('Either default buffer or dtype must be provided')
            if type(self.default) == float:
                self.dtype = ClTypes.double
            elif type(self.default) == int:
                self.dtype = ClTypes.int
            else:
                raise NotImplementedError(f'Automatic scalar conversion for type {type(self.default)} is not supported')


@dataclass
class OrderInMemory:
    C_CONTIGUOUS: str = 'c_contiguous'
    F_CONTIGUOUS: str = 'f_contiguous'


@dataclass
class KnlArgBuffer(ArgBase):
    default: Array = None
    address_space_qualifier: str = field(default='')
    b_return_buffer: bool = field(default=False)
    dtype: np.dtype = None  # if None extracted from default array
    order_in_memory: str = OrderInMemory.C_CONTIGUOUS

    def __post_init__(self):
        if self.address_space_qualifier != '__constant':
            self.address_space_qualifier = '__global {}'.format(self.address_space_qualifier)
        if self.dtype is None:
            if self.default is None:
                raise ValueError('Either default buffer or dtype must be provided')
            self.dtype = self.default.dtype


def template(func: Union['ClKernel', 'ClFunction']) -> str:
    body = func.body if type(func.body) == str else ''.join(func.body)
    tpl = func.header + '\n{' + body + '}\n'
    args = [value.to_string(key) + ',' for key, value in func.args.items()]
    args = '{}'.format('\n'.join(args))
    args = args[:-1]  # remove last comma
    replacements = {'name': func.name,
                    'args': args,
                    'return_type': c_name_from_dtype(func.return_type)}
    for key, value in func.replacements.items():
        replacements[key] = str(value)
    try:  # todo: e.g. if replacement has been forgotten, still save template as file
        tpl = Template(tpl).render(**replacements)
    except:
        raise ValueError(exceptions.text_error_template().render())
    tpl_formatted = pyastyle.format(tpl, '--style=allman --indent=spaces=4')
    return tpl_formatted


@dataclass
class ClFunctionBase(ABC):
    name: str = 'func'

    @property
    def header(self):
        return '${return_type} ${name}(${args})'

    @property
    @abstractmethod
    def template(self) -> str:
        pass


@dataclass
class ClFunction(ClFunctionBase):
    @property
    def template(self) -> str:
        return template(self)

    args: Dict[str, Union[ArgScalar, ArgBuffer, ArgLocal, ArgPrivate, ArgConstant]] = field(default_factory=lambda: [])
    body: Union[List[str], str] = field(default_factory=lambda: [])
    replacements: Dict[str, Union[str, float, int, bool]] = field(default_factory=lambda: {})
    return_type: np.dtype = field(default_factory=lambda: np.dtype(np.void))
    type_defs: Dict[str, np.dtype] = field(default_factory=lambda: {})  # todo
    defines: Dict[str, ScalarArgTypes] = field(default_factory=lambda: {})

    def __post_init__(self):
        if isinstance(self.body, str):
            self.body = [self.body]


KnlGridType = Union[Tuple[int], Tuple[int, int], Tuple[int, int, int]]
KnlReplacementTypes = Union[str, float, int, bool]


class Compilable:
    @abstractmethod
    def compile(self, cl_init: ClInit, b_python: bool = False):
        pass


@dataclass
class ClKernel(ClFunctionBase, Compilable):
    def compile(self, cl_init, b_python: bool = False, file='$default_path'):
        return ClProgram(kernels=[self]).compile(cl_init=cl_init, b_python=b_python, file=file).__getattr__(self.name)
        # return compile_cl_kernel(self, cl_init, b_python=b_python, file=file)

    args: Dict[str, Union[np.ndarray, KnlArgBuffer, KnlArgScalar, ArgScalar, ArgBuffer]] = field(
        default_factory=lambda: [])
    body: Union[List[str], str] = field(default_factory=lambda: [])
    replacements: Dict[str, KnlReplacementTypes] = field(default_factory=lambda: {})
    global_size: KnlGridType = None
    local_size: KnlGridType = None
    type_defs: Dict[str, np.dtype] = field(default_factory=lambda: {})
    defines: Dict[str, ScalarArgTypes] = field(default_factory=lambda: {})

    def __post_init__(self):
        if isinstance(self.body, str):
            self.body = [self.body]

    @property
    def template(self) -> str:
        return template(self)

    @property
    def return_type(self):
        return np.dtype(np.void)

    @property
    def header(self):
        return '__kernel ${return_type} ${name}(${args})'


@dataclass
class ClProgram(Compilable):
    """
    Models an OpenCl Program containing functions or kernels.
    """

    def compile(self, cl_init: ClInit, b_python: bool = False,
                file: str = '$default_path') -> 'ProgramContainer':

        return compile_cl_program(self, cl_init, b_python, file)

    functions: List[ClFunction] = field(default_factory=lambda: [])
    kernels: List[ClKernel] = field(default_factory=lambda: [])
    defines: Dict[str, ScalarArgTypes] = field(default_factory=lambda: {})
    type_defs: Dict[str, np.dtype] = field(default_factory=lambda: {})

    @property
    def rendered_template(self):
        functions = [f.template for f in self.functions] + [k.template for k in self.kernels]
        functions = '\n'.join(functions)

        if 'double' in functions:
            _preamble_precision = preamble_precision('double')
        else:
            _preamble_precision = preamble_precision('single')

        if 'cfloat_t' in functions:
            _preamble_generic_operations = preamble_generic_type_operations('complex', 'single')
        elif 'cdouble_t' in functions:
            _preamble_generic_operations = preamble_generic_type_operations('complex', 'double')
        else:
            _preamble_generic_operations = preamble_generic_type_operations('real')
        preamble_buff_t = f'{_preamble_precision}\n{_preamble_generic_operations}'

        # join program typedefs with typedefs from kernels and functions
        # todo: consider replacing type strings directly to avoid name conflicts
        def update_and_checks_for_duplicates_same_type(items: dict, dic: dict):
            for key, value in items.items():
                if key in dic:
                    if not dic[key] == value:
                        raise ValueError('Same type def name for different types')
                else:
                    dic[key] = value

        [update_and_checks_for_duplicates_same_type(func.type_defs, self.type_defs) for func in self.functions]
        [update_and_checks_for_duplicates_same_type(func.type_defs, self.type_defs) for func in self.kernels]

        [update_and_checks_for_duplicates_same_type(func.defines, self.defines) for func in self.functions]
        [update_and_checks_for_duplicates_same_type(func.defines, self.defines) for func in self.kernels]

        defines = '\n'.join(['#define {} {}'.format(key, str(value)) for key, value in self.defines.items()])

        type_defs = '\n'.join(
            ['typedef {c_name} {new_name};\n#define convert_{new_name}(x) convert_{c_name}(x)'.format(
                c_name=c_name_from_dtype(value),
                new_name=str(key))
                for key, value in self.type_defs.items()])

        tpl_all = self._get_tpl(preamble_buff_t, defines, type_defs, functions)

        tpl_formatted = pyastyle.format(tpl_all, '--style=allman --indent=spaces=4')
        return tpl_formatted

    def _get_tpl(self, preamble_complex, defines, type_defs, functions):
        return '{}\n\n{}\n\n{}\n\n{}\n\n'.format(preamble_complex, defines, type_defs, functions)


def build_for_device(context: Context, template_to_be_compiled: str, file: str = None) -> Program:
    if file is not None:
        write_string_to_file(template_to_be_compiled, file + '.cl', b_logging=False)
    try:
        program = Program(context, str(template_to_be_compiled)).build()
    except Exception as error:
        tpl_line_numbers = '\n'.join(
            ['{:<4}{}'.format(i + 1, line) for i, line in enumerate(template_to_be_compiled.split('\n'))])
        raise ValueError('\n{}\n\n{}'.format(tpl_line_numbers, str(error)))
    return program


KernelCallReturnType = Union[Array, Tuple[Array, ...], None]


# Todo: Find good structure for modeling cl and python kernels
@dataclass
class CallableKernel(ABC):
    kernel_model: ClKernel

    def __getattr__(self, name):
        if name in self.kernel_model.args.keys():
            return self.kernel_model.args[name].default
        return super().__getattribute__(name)

    @abstractmethod
    def __call__(self, global_size: KnlGridType,
                 local_size: KnlGridType = None,
                 **kwargs):
        pass

    @staticmethod
    def _typing_scalar_argument(arg_model: Union[KnlArgScalar, ArgScalar],
                                scalar_value_provided: ScalarArgTypes):
        if get_vec_size(arg_model.dtype) == 1:
            return np.dtype(arg_model.dtype).type(scalar_value_provided)
        else:
            dtype_scalar = scalar_type_from_vec_type(arg_model.dtype)
            scalar = dtype_scalar.type(scalar_value_provided)  # converts to bytes like object
            return scalar.astype(arg_model.dtype)  # converts to vector type

    @staticmethod
    def _prepare_arguments(knl: ClKernel, **kwargs):
        global_size = kwargs.pop('global_size', None)
        local_size = kwargs.pop('local_size', None)
        if global_size is None:
            global_size = knl.global_size
        if local_size is None:
            local_size = knl.local_size
        supported_kws = [k for k in knl.args.keys()]
        kw_not_in_kernel_arguments = [kw for kw in kwargs if kw not in supported_kws]
        if len(kw_not_in_kernel_arguments) > 0:
            raise ValueError(
                f'keyword argument {kw_not_in_kernel_arguments} does not exist in kernel argument list {supported_kws}')
        # set default arguments. Looping over kernel model forces correct order of arguments
        args_call = [kwargs.pop(key, value.default if isinstance(value, (KnlArgBuffer, KnlArgScalar)) else None)
                     for key, value in knl.args.items()]
        if any(arg is None for arg in args_call):
            raise ValueError('Argument equal to None can lead to system crash')

        if global_size is None:
            raise ValueError('global_size not provided!')
        if global_size == ():
            raise ValueError('Global size is empty')
        if 0 in global_size:
            raise ValueError(f'Parameter in global size {global_size} equal to zero')
        if local_size is not None and 0 in local_size:
            raise ValueError(f'Parameter in local size {local_size} equal to zero')
        # convert scalar argument to correct type. E.g. argument can be python int and is converted to char
        args_model = list(knl.args.values())
        args_call = [CallableKernel._typing_scalar_argument(args_model[i], arg)
                     if type(args_model[i]) in [KnlArgScalar, ArgScalar] else arg
                     for i, arg in enumerate(args_call)]
        # check if buffer have same type as defined in the kernel function header
        b_types_equal = [args_call[i].dtype == v.dtype for i, v in enumerate(args_model) if isinstance(v, KnlArgBuffer)]
        if not np.all(b_types_equal):
            idx_buffer_list = int(np.argmin(b_types_equal))
            idx = [i for i, kv in enumerate(knl.args.items()) if isinstance(kv[1], KnlArgBuffer)][idx_buffer_list]
            buffer_name = [k for k, v in knl.args.items()][idx]
            buffer_type_expected = args_model[idx].dtype
            buffer_type_call = args_call[idx].dtype
            raise ValueError(f'Expected buffer argument \'{buffer_name}\' with type {buffer_type_expected} '
                             f'but got buffer with type {buffer_type_call}')

        # check if buffer elements of array arguments have memory order as expected (c or f contiguous)
        def b_array_memory_order_as_expected(ary_model: KnlArgBuffer, ary_call: Array):
            if ary_model.order_in_memory == OrderInMemory.C_CONTIGUOUS:
                return ary_call.flags.c_contiguous
            else:  # f_contiguous
                return ary_call.flags.f_contiguous

        knl_args_invalid_memory_order = [(k, v) for idx, (k, v) in enumerate(knl.args.items())
                                         if isinstance(v, KnlArgBuffer) and
                                         not b_array_memory_order_as_expected(v, args_call[idx])]
        if len(knl_args_invalid_memory_order) > 0:
            msg = '\n'.join([f'Array argument \'{arg[0]}\' is not {arg[1].order_in_memory} (as defined in ClKernel)'
                             for arg in knl_args_invalid_memory_order])
            raise ValueError(msg)
        non_supported_types = [np.ndarray]
        if any(_ := [type(arg) in non_supported_types for arg in args_call]):
            raise ValueError(f'Type of argument \'{list(knl.args.items())[np.where(_)[0][0]][0]}\' is not supported in '
                             f'kernel call')
        return global_size, local_size, args_call

    @staticmethod
    def _extract_return_arguments_from_source_program(knl: ClKernel):
        args_model = list(knl.args.values())
        return [arg.default for arg in args_model if type(arg) == KnlArgBuffer and arg.b_return_buffer]


@dataclass
class CallableKernelEmulation(CallableKernel):
    function: Callable

    def __call__(self,
                 global_size: KnlGridType = None,
                 local_size: KnlGridType = None,
                 **kwargs: Union[Array, object]) -> KernelCallReturnType:
        # e.g. if two kernels of a program shall run concurrently, this can be enable by passing another queue here
        if 'queue' in kwargs:  # currently queue kwarg is not considered in emulation
            _ = kwargs.pop('queue')
        global_size, local_size, args = self._prepare_arguments(self.kernel_model, global_size=global_size,
                                                                local_size=local_size, **kwargs)
        self.function(global_size, local_size, *args)
        return_args = self._extract_return_arguments_from_source_program(self.kernel_model)
        if len(return_args) == 0:
            return None
        elif len(return_args) == 1:
            return return_args[0]
        else:
            return tuple(return_args)


@dataclass
class CallableKernelDevice(CallableKernel):
    compiled: Kernel
    queue: CommandQueueExtended

    def __call__(self,
                 global_size: KnlGridType = None,
                 local_size: KnlGridType = None,
                 **kwargs):
        # e.g. if two kernels of a program shall run concurrently, this can be enable by passing another queue here
        if 'queue' in kwargs:
            queue = kwargs.pop('queue')
        else:
            queue = self.queue
        global_size, local_size, args = self._prepare_arguments(self.kernel_model, global_size=global_size,
                                                                local_size=local_size, **kwargs)
        # extract buffer from cl arrays separate, since in emulation we need cl arrays
        args_cl = [arg.data if isinstance(arg, Array) else arg for i, arg in enumerate(args)]
        event = self.compiled(queue, global_size, local_size, *args_cl)

        if queue.properties == command_queue_properties.PROFILING_ENABLE:
            if len(queue.events) < int(1e6):
                queue.events.append((self.kernel_model.name, event))
            else:
                raise ValueError('Forgot to disable profiling?')

        return_args = self._extract_return_arguments_from_source_program(self.kernel_model)
        if len(return_args) == 0:
            return None
        elif len(return_args) == 1:
            return return_args[0]
        else:
            return tuple(return_args)


@dataclass
class ProgramContainer:
    """
    Responsibility:
    A callable kernel is returned with program.kernel_name. Depending on value of b_run_python_emulation a call of this
    kernel is executed on device or in emulation.
    """
    program_model: ClProgram
    file: str
    init: ClInit
    callable_kernels: Dict[str, Union[CallableKernelEmulation, CallableKernelDevice]] = None

    def __getattr__(self, name) -> CallableKernel:
        if name in self.callable_kernels:
            return self.callable_kernels[name]
        else:
            return super().__getattribute__(name)


class ClHelpers:
    """
    Responsibility:
    This class forces any child class to use a framework for writing kernel code using a OOP data structure.
    This framework enables to emulate the kernel execution in Python for e.g. debugging purposes.
    The child class has to call self.post_init() to build program.
    """
    @staticmethod
    def _camel_to_snake(name):
        name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()

    # helper methods which can be used in subclasses
    @staticmethod
    def command_compute_address(n_dim: int) -> str:
        command = '0'
        for i in range(n_dim):
            offset = '1'
            for j in range(i + 1, n_dim):
                offset += '*get_global_size({})'.format(j)
            command += '+get_global_id({})*{}'.format(i, offset)
        return command

    # helpers for using vector types

    @staticmethod
    def get_vec_dtype(dtype_vec: np.dtype, dtype_scalar: np.dtype) -> np.dtype:
        if number_vec_elements_of_cl_type(dtype_vec) == 1:
            return dtype_scalar
        else:
            c_name = '{}{}'.format(c_name_from_dtype(dtype_scalar), number_vec_elements_of_cl_type(dtype_vec))
            return getattr(ClTypes, c_name)

    @staticmethod
    def array_indexing_for_vec_type(array: str, index: str, dtype: np.dtype):
        """
        https://stackoverflow.com/questions/24746221/using-a-vector-as-an-array-index
        e.g.
        uchar4 keys = (uchar4)(5, 0, 2, 6);
        uint4 results = (uint4)(data[keys.s0], data[keys.s1], data[keys.s2], data[keys.s3]);

        :param dtype:
        :param array:
        :param index:
        :return:
        """
        if number_vec_elements_of_cl_type(dtype) == 1:
            return '{array_name}[{index_name}]'.format(array_name=array, index_name=index)
        else:
            return '({c_type_name})({vector})'.format(c_type_name=c_name_from_dtype(dtype),
                                                      vector=', '.join(
                                                          ['{array_name}[{index_name}.s{i_vec_element}]'.format(
                                                              array_name=array,
                                                              index_name=index,
                                                              i_vec_element=np_cl_types.VEC_INDICES[i])
                                                              for i in range(number_vec_elements_of_cl_type(dtype))]))

    @staticmethod
    def command_const_vec_type(param: Union[str, float, int], dtype: np.dtype) -> str:
        """
        param = 1.5, dtype=ClTypes.float -> 'convert_float(1.5)'
        param = 1.5, dtype=ClTypes.float2 -> '(float2)(convert_float(1.5), convert_float(1.5))

        :param param:
        :param dtype:
        :return:
        """
        if number_vec_elements_of_cl_type(dtype) == 1:
            return 'convert_{}({})'.format(c_name_from_dtype(dtype), param)
        else:
            dtype_c_name = c_name_from_dtype(scalar_type_from_vec_type(dtype))
            return '({})(({}))'.format(c_name_from_dtype(dtype),
                                       ', '.join(['convert_{}({})'.format(dtype_c_name,
                                                                          param)] * get_vec_size(dtype)))

    @staticmethod
    def command_vec_sum(var_name: str, dtype: np.dtype) -> str:
        """
        Cases:
        float var_name -> return 'var_name'
        float4 var_name -> return 'var_name.s0 + var_name.s1 + var_name.s2 + var_name.s3'
        :param var_name:
        :return:
        """
        if np_cl_types.get_vec_size(dtype) == 1:
            return var_name
        else:
            return ' + '.join(
                ['{}.s{}'.format(var_name, np_cl_types.VEC_INDICES[i]) for i in range(get_vec_size(dtype))])

    # todo: use splay method of pyopencl library instead
    # from pyopencl.array import splay
    # splay
    @staticmethod
    def _get_local_size_coalesced_last_dim(global_size, desired_wg_size):
        """
        E.g. global_size = (1000, 25) and desired_wg_size=64
        Then a local_size=(2,25) is returned for multiple reasons:

        The work group size must be equal or smaller than the desired work group size.
        We make the last local dimension is large as possible (cannot exceed global size of last dimension).
        If possible the second last dimension is set to a value larger than 1, such that we get close to our desired
        work group size.

        :param global_size:
        :param desired_wg_size:
        :return:
        """

        local_size = [1] * len(global_size)
        for i_dim in range(1, len(global_size) + 1):
            if global_size[-i_dim] * local_size[-i_dim + 1] < desired_wg_size:
                local_size[-i_dim] = global_size[-i_dim]
            else:
                local_size[-i_dim] = np.max([i for i in range(1, desired_wg_size + 1)
                                             if (global_size[-i_dim] / i).is_integer() and
                                             i * local_size[-i_dim + 1] <= desired_wg_size])
        if np.product(local_size) < desired_wg_size:
            pass
            # res = inspect.stack()
            # logging.info(f'Local size {local_size} is suboptimal for desired work group size of {desired_wg_size}. '
            #              f'For best performance increase the global size of the most inner dimension, until it is '
            #              f'divisible by {desired_wg_size}. \n'
            #              f'More information: '
            #              f'https://stackoverflow.com/questions/3957125/questions-about-global-and-local-work-size')
        return tuple(local_size)
        # return None

    @staticmethod
    def get_local_size_coalesced_last_dim(global_size, cl_init: ClInit):
        """
        If global size is no multiple of the local size, according to following link it should not work.
        https://community.khronos.org/t/opencl-ndrange-global-size-local-size/4167

        However (only for AMD GPU), simple tests have shown that it still works. Therefore this class gives a local size, where the global
        size is not necessarily a multiple.

        :param global_size:
        :param cl_init:
        :return:
        """
        desired_wg_size = 4 * cl_init.device.global_mem_cacheline_size
        return ClHelpers._get_local_size_coalesced_last_dim(global_size, desired_wg_size)


# https://stackoverflow.com/questions/1988804/what-is-memoization-and-how-can-i-use-it-in-python
class MemoizeKernelFunctions:
    def __init__(self, f):
        self.f = f
        self.memo = {}

    def __call__(self, program_model: ClProgram, cl_init: ClInit, file: str = None):
        body = ''.join(program_model.rendered_template)
        _id = hash(f'{cl_init.context.int_ptr}{cl_init.queue.int_ptr}{body}')
        if _id not in self.memo:
            self.memo[_id] = self.f(program_model, cl_init, file)
        return self.memo[_id]


@MemoizeKernelFunctions
def compile_cl_program_device(program_model: ClProgram, cl_init: ClInit = None, file: str = None) -> Dict[str, Kernel]:
    context = cl_init.context
    queue = cl_init.queue
    code_cl = program_model.rendered_template
    program = build_for_device(context, code_cl, file)
    kernels_model = program_model.kernels
    # set scalar arguments for each kernel from kernel model
    callable_kernels = {knl.function_name: knl
                        for i, knl in enumerate(program.all_kernels())}
    for i, knl in enumerate(kernels_model):
        arg_types = [arg.dtype if type(arg) in [KnlArgScalar, ArgScalar] else None
                     for _, arg in kernels_model[i].args.items()]
        callable_kernels[knl.name].set_scalar_arg_dtypes(arg_types)
    return callable_kernels


@MemoizeKernelFunctions
def compile_cl_program_emulation(program_model: ClProgram, cl_init: ClInit, file: str = None) -> Dict[str,
                                                                                                      Callable]:
    code_py = unparse_c_code_to_python(program_model.rendered_template)
    module = create_py_file_and_load_module(code_py, file)
    kernels_model = program_model.kernels
    callable_kernels = {knl.name: module.__getattribute__(knl.name) for knl in kernels_model}
    return callable_kernels


def compile_cl_program(program_model: ClProgram, cl_init: ClInit = None, b_python: bool = False,
                       file: str = '$default_path') -> ProgramContainer:
    # deal with file name
    if isinstance(file, Path):
        file = str(file)
    if file is None and b_python:
        raise ValueError('You intended to create no file by setting file=None. '
                         'However, a file must be created for debugging.')  # todo can python debugging run without file?
    elif file == '$default_path':
        file = str(Path(os.getcwd()).joinpath('py_cl_kernels').joinpath(program_model.kernels[0].name))

    # try to extract cl init from kernel buffer default arguments. This improves usability
    if cl_init is None:
        try:
            knl_arg_buffer = [v for k, v in program_model.kernels[0].args.items()
                              if isinstance(v, KnlArgBuffer) and v.default is not None][0]
            cl_init = ClInit(context=knl_arg_buffer.default.context, queue=knl_arg_buffer.default.queue)
        except IndexError:  # when no default value is present index error is raised
            cl_init = ClInit()

    # If kernel arguments are of type np.ndarray they are converted to cl arrays here
    # This is done here, since cl_init is available at this point for sure.
    for knl in program_model.kernels:
        knl.args = {k: KnlArgBuffer(to_device(cl_init.queue, v)) if isinstance(v, np.ndarray) else v
                    for k, v in knl.args.items()}
        knl.args = {k: KnlArgScalar(default=v) if isinstance(v, ScalarArgTypes.__args__) else v
                    for k, v in knl.args.items()}

    dict_kernels_program_model = {knl.name: knl for knl in program_model.kernels}
    if b_python:
        dict_emulation_kernel_functions = compile_cl_program_emulation(program_model, cl_init, file)
        callable_kernels = {k: CallableKernelEmulation(kernel_model=dict_kernels_program_model[k], function=v)
                            for k, v in dict_emulation_kernel_functions.items()}
    else:
        dict_device_kernel_functions = compile_cl_program_device(program_model, cl_init, file)
        callable_kernels = {k: CallableKernelDevice(kernel_model=dict_kernels_program_model[k], compiled=v,
                                                    queue=cl_init.queue)
                            for k, v in dict_device_kernel_functions.items()}

    return ProgramContainer(program_model=program_model,
                            file=file,
                            init=cl_init,
                            callable_kernels=callable_kernels)