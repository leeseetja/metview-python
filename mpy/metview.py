
import io
import os
import builtins
import tempfile
import signal

import cffi
import pandas as pd


def read(fname):
    file_path = os.path.join(os.path.dirname(__file__), fname)
    return io.open(file_path, encoding='utf-8').read()


# Python uses 0-based indexing, Metview uses 1-based indexing
def python_to_mv_index(pi):
    return pi + 1


def string_from_ffi(s):
    return ffi.string(s).decode('utf-8')


class MetviewInvoker:
    """Starts a new Metview session on construction and terminates it on program exit"""

    def __init__(self):
        """
        Constructor - starts a Metview session and reads its environment information
        Raises an exception if Metview does not respond within 5 seconds
        """

        # check whether we're in a running Metview session
        if 'METVIEW_TITLE_PROD' in os.environ:
            self.persistent_session = True
            self.info_section = {'METVIEW_LIB': os.environ['METVIEW_LIB']}
            return

        import atexit
        import time
        import subprocess

        print('MetviewInvoker: Invoking Metview')
        self.persistent_session = False
        self.metview_replied = False
        self.metview_startup_timeout = 5  # seconds

        # start Metview with command-line parameters that will let it communicate back to us
        env_file = tempfile.NamedTemporaryFile(mode='rt')
        pid = os.getpid()
        # print('PYTHON:', pid, ' ', env_file.name, ' ', repr(signal.SIGUSR1))
        signal.signal(signal.SIGUSR1, self.signal_from_metview)
        # p = subprocess.Popen(['metview', '-edbg', 'tv8 -a', '-slog', '-python-serve', env_file.name, str(pid)], stdout=subprocess.PIPE)
        # p = subprocess.Popen(['metview', '-slog', '-python-serve', env_file.name, str(pid)])
        subprocess.Popen(['metview', '-python-serve', env_file.name, str(pid)], stdout=subprocess.PIPE)

        # wait for Metview to respond...
        wait_start = time.time()
        while not(self.metview_replied) and (time.time() - wait_start < self.metview_startup_timeout):
            time.sleep(0.001)

        if not(self.metview_replied):
            raise Exception('Command "metview" did not respond before ' + str(self.metview_startup_timeout) + ' seconds')

        self.read_metview_settings(env_file.name)

        # when the Python session terminates, we should destroy this object so that the Metview
        # session is properly cleaned up. We can also do this in a __del__ function, but there can
        # be problems with the order of cleanup - e.g. the 'os' module might be deleted before
        # this destructor is called.
        atexit.register(self.destroy)

    def destroy(self):
        """Kills the Metview session. Raises an exception if it could not do it."""

        if self.persistent_session:
            return

        if self.metview_replied:
            print('MetviewInvoker: Closing Metview')
            metview_pid = self.info('EVENT_PID')
            try:
                os.kill(int(metview_pid), signal.SIGUSR1)
            except Exception as exp:
                print("Could not terminate the Metview process pid=" + metview_pid)
                raise exp

    def signal_from_metview(self, *args):
        """Called when Metview sends a signal back to Python to say that it's started"""
        # print ('PYTHON: GOT SIGNAL BACK FROM METVIEW!')
        self.metview_replied = True

    def read_metview_settings(self, settings_file):
        """Parses the settings file generated by Metview and sets the corresponding env vars"""
        import configparser

        cf = configparser.ConfigParser()
        cf.read(settings_file)
        env_section = cf['Environment']
        for envar in env_section:
            # print('set ', envar.upper(), ' = ', env_section[envar])
            os.environ[envar.upper()] = env_section[envar]
        self.info_section = cf['Info']

    def info(self, key):
        """Returns a piece of Metview information that was not set as an env var"""
        return self.info_section[key]


mi = MetviewInvoker()

try:
    ffi = cffi.FFI()
    ffi.cdef(read('metview.h'))
    mv_lib = mi.info('METVIEW_LIB')
    # is there a more general way to add to a path?
    os.environ["LD_LIBRARY_PATH"] = mv_lib + ':' + os.environ.get("LD_LIBRARY_PATH", '')
    lib = ffi.dlopen(os.path.join(mv_lib, 'libMvMacro.so'))
    lib.p_init()
except Exception as exp:
    print('Error loading Metview package. LD_LIBRARY_PATH=' + os.environ["LD_LIBRARY_PATH"])
    raise exp


class Value:

    def __init__(self, val_pointer):
        self.val_pointer = val_pointer

    def push(self):
        return self.val_pointer


class Request(dict, Value):
    verb = "UNKNOWN"

    def __init__(self, req):
        self.val_pointer = None

        if isinstance(req, dict):
            self.update(req)
            self.to_metview_style()
            if isinstance(req, Request):
                self.verb = req.verb
        else:
            Value.__init__(self, req)
            self.verb = string_from_ffi(lib.p_get_req_verb(req))
            n = lib.p_get_req_num_params(req)
            for i in range(0, n):
                param = string_from_ffi(lib.p_get_req_param(req, i))
                raw_val = lib.p_get_req_value(req, param.encode('utf-8'))
                if raw_val != ffi.NULL:
                    val = string_from_ffi(raw_val)
                    self[param] = val
            # self['_MACRO'] = 'BLANK'
            # self['_PATH']  = 'BLANK'

    def __str__(self):
        return "VERB: " + self.verb + super().__str__()

    # translate Python classes into Metview ones where needed
    def to_metview_style(self):
        for k, v in self.items():

            # if isinstance(v, (list, tuple)):
            #    for v_i in v:
            #        v_i = str(v_i).encode('utf-8')
            #        lib.p_add_value(r, k.encode('utf-8'), v_i)

            if isinstance(v, bool):
                conversion_dict = {True: 'on', False: 'off'}
                self[k] = conversion_dict[v]

    def push(self):
        # if we have a pointer to a Metview Value, then use that because it's more
        # complete than the dict
        if self.val_pointer:
            lib.p_push_request(Value.push(self))
        else:
            r = lib.p_new_request(self.verb.encode('utf-8'))

            # to populate a request on the Macro side, we push each
            # value onto its stack, and then tell it to create a new
            # parameter with that name for the request. This allows us to
            # use Macro to handle the addition of complex data types to
            # a request
            for k, v in self.items():
                push_arg(v, 'NONAME')
                lib.p_set_request_value_from_pop(r, k.encode('utf-8'))

            lib.p_push_request(r)

    def __getitem__(self, index):
        # we don't often need integer indexing of requests, but we do in the
        # case of a Display Window object
        if isinstance(index, int):
            return subset(self, python_to_mv_index(index))
        else:
            return subset(self, index)


# def dict_to_request(d, verb='NONE'):
#    # get the verb from the request if not supplied by the caller
#    if verb == 'NONE' and isinstance(d, Request):
#        verb = d.verb
#
#    r = lib.p_new_request(verb.encode('utf-8'))
#    for k, v in d.items():
#        if isinstance(v, (list, tuple)):
#            for v_i in v:
#                v_i = str(v_i).encode('utf-8')
#                lib.p_add_value(r, k.encode('utf-8'), v_i)
#        elif isinstance(v, (Fieldset, Bufr, Geopoints)):
#            lib.p_set_value(r, k.encode('utf-8'), v.push())
#        elif isinstance(v, str):
#            lib.p_set_value(r, k.encode('utf-8'), v.encode('utf-8'))
#        elif isinstance(v, bool):
#            conversion_dict = {True: 'on', False: 'off'}
#            lib.p_set_value(r, k.encode('utf-8'), conversion_dict[v].encode('utf-8'))
#        elif isinstance(v, (int, float)):
#            lib.p_set_value(r, k.encode('utf-8'), str(v).encode('utf-8'))
#        else:
#            lib.p_set_value(r, k.encode('utf-8'), v)
#    return r


# def push_dict(d, verb='NONE'):
#
#    for k, v in d.items():
#        if isinstance(v, (list, tuple)):
#            for v_i in v:
#                v_i = str(v_i).encode('utf-8')
#                lib.p_add_value(r, k.encode('utf-8'), v_i)
#        elif isinstance(v, (Fieldset, Bufr, Geopoints)):
#            lib.p_set_value(r, k.encode('utf-8'), v.push())
#        elif isinstance(v, str):
#            lib.p_set_value(r, k.encode('utf-8'), v.encode('utf-8'))
#        elif isinstance(v, bool):
#            conversion_dict = {True: 'on', False: 'off'}
#            lib.p_set_value(r, k.encode('utf-8'), conversion_dict[v].encode('utf-8'))
#        elif isinstance(v, (int, float)):
#            lib.p_set_value(r, k.encode('utf-8'), str(v).encode('utf-8'))
#        else:
#            lib.p_set_value(r, k.encode('utf-8'), v)
#    return r


def push_bytes(b):
    lib.p_push_string(b)


def push_str(s):
    push_bytes(s.encode('utf-8'))


def push_list(lst):
    # ask Metview to create a new list, then add each element by
    # pusing it onto the stack and asking Metview to pop it off
    # and add it to the list
    mlist = lib.p_new_list(len(lst))
    for i, val in enumerate(lst):
        push_arg(val, 'NONE')
        lib.p_add_value_from_pop_to_list(mlist, i)
    lib.p_push_list(mlist)


def push_arg(n, name):

    nargs = 1

    if isinstance(n, float):
        lib.p_push_number(n)
    elif isinstance(n, int):
        lib.p_push_number(float(n))
    elif isinstance(n, str):
        push_str(n)
    elif isinstance(n, Request):
        n.push()
    elif isinstance(n, dict):
        Request(n).push()
    elif isinstance(n, Fieldset):
        lib.p_push_value(n.push())
    elif isinstance(n, Bufr):
        lib.p_push_value(n.push())
    elif isinstance(n, Geopoints):
        lib.p_push_value(n.push())
    elif isinstance(n, NetCDF):
        lib.p_push_value(n.push())
    elif isinstance(n, (list, tuple)):
        push_list(n)
    else:
        raise TypeError('Cannot push this type of argument to Metview: ', builtins.type(n))

    return nargs


def dict_to_pushed_args(d):

    # push each key and value onto the argument stack
    for k, v in d.items():
        push_str(k)
        push_arg(v, 'NONE')

    return 2 * len(d)  # return the number of arguments generated


class FileBackedValue(Value):

    def __init__(self, val_pointer):
        Value.__init__(self, val_pointer)
        # ask Metview for the file relating to this data (Metview will write it if necessary)
        self.url = string_from_ffi(lib.p_data_path(val_pointer))


class Fieldset(FileBackedValue):

    def __init__(self, val_pointer):
        FileBackedValue.__init__(self, val_pointer)

    def __add__(self, other):
        return add(self, other)

    def __sub__(self, other):
        return sub(self, other)

    def __mul__(self, other):
        return prod(self, other)

    def __truediv__(self, other):
        return div(self, other)

    def __pow__(self, other):
        return power(self, other)

    def __len__(self):
        return int(count(self))

    def __getitem__(self, index):
        return subset(self, python_to_mv_index(index))

    def to_dataset(self):
        # soft dependency on xarray_grib
        try:
            import xarray_grib
            import xarray as xr
        except ImportError:
            print("Package xarray_grib not found. Try running 'pip install xarray_grib'.")
            raise
        store = xarray_grib.GribDataStore(self.url)
        dataset = xr.open_dataset(store)
        return dataset


class Bufr(FileBackedValue):

    def __init__(self, val_pointer):
        FileBackedValue.__init__(self, val_pointer)


class Geopoints(FileBackedValue):

    def __init__(self, val_pointer):
        FileBackedValue.__init__(self, val_pointer)

    def __mul__(self, other):
        return prod(self, other)

    def __ge__(self, other):
        return greater_equal_than(self, other)

    def __gt__(self, other):
        return greater_than(self, other)

    def __le__(self, other):
        return lower_equal_than(self, other)

    def __lt__(self, other):
        return lower_than(self, other)

    def __add__(self, other):
        return add(self, other)

    def filter(self, other):
        return filter(self, other)

    def to_dataframe(self):
        return pd.read_table(self.url, skiprows=3)


class NetCDF(FileBackedValue):
    def __init__(self, val_pointer):
        FileBackedValue.__init__(self, val_pointer)

    def __add__(self, other):
        return add(self, other)

    def __sub__(self, other):
        return sub(self, other)

    def __mul__(self, other):
        return prod(self, other)

    def __truediv__(self, other):
        return div(self, other)

    def __pow__(self, other):
        return power(self, other)


def list_from_metview(mlist):

    result = []
    n = lib.p_list_count(mlist)
    for i in range(0, n):
        mval = lib.p_list_element_as_value(mlist, i)
        v = value_from_metview(mval)
        result.append(v)
    return result


# we can actually get these from Metview, but for testing we just have a dict
# service_function_verbs = {
#     'retrieve': 'RETRIEVE',
#     'mcoast': 'MCOAST',
#     'mcont': 'MCONT',
#     'mobs': 'MOBS',
#     'msymb': 'MSYMB',
#     'read': 'READ',
#     'geoview': 'GEOVIEW',
#     'mtext': 'MTEXT',
#     'ps_output': 'PS_OUTPUT',
#     'obsfilter': 'OBSFILTER',
#     'filter': 'FILTER'
# }


def _call_function(name, *args, **kwargs):

    nargs = 0

    for n in args:
        actual_n_args = push_arg(n, name)
        nargs += actual_n_args

    merged_dict = {}
    merged_dict.update(kwargs)
    if len(merged_dict) > 0:
        dn = dict_to_pushed_args(Request(merged_dict))
        nargs += dn

    lib.p_call_function(name.encode('utf-8'), nargs)


def value_from_metview(val):
    rt = lib.p_value_type(val)
    # Number
    if rt == 0:
        return lib.p_value_as_number(val)
    # String
    elif rt == 1:
        return string_from_ffi(lib.p_value_as_string(val))
    # Fieldset
    elif rt == 2:
        return Fieldset(val)
    # Request dictionary
    elif rt == 3:
        return_req = lib.p_value_as_request(val)
        return Request(return_req)
    # BUFR
    elif rt == 4:
        return Bufr(val)
    # Geopoints
    elif rt == 5:
        return Geopoints(val)
    # list
    elif rt == 6:
        return list_from_metview(lib.p_value_as_list(val))
    # netCDF
    elif rt == 7:
        return NetCDF(val)
    elif rt == 8:
        return None
    elif rt == 9:
        err_msg = string_from_ffi(lib.p_error_message(val))
        raise Exception('Metview error: ' + err_msg)
    else:
        raise Exception('value_from_metview got an unhandled return type')


def make(name):

    def wrapped(*args, **kwargs):
        err = _call_function(name, *args, **kwargs)
        if err:
            pass  # throw Exceception

        val = lib.p_result_as_value()
        return value_from_metview(val)

    return wrapped


abs = make('abs')
accumulate = make('accumulate')
add = make('+')
base_date = make('base_date')
count = make('count')
dimension_names = make('dimension_names')
distance = make('distance')
div = make('/')
describe = make('describe')
filter = make('filter')
geoview = make('geoview')
greater_equal_than = make('>=')
greater_than = make('>')
grib_get_string = make('grib_get_string')
grib_get_long = make('grib_get_long')
interpolate = make('interpolate')
low = make('lowercase')
lower_equal_than = make('<=')
lower_than = make('<')
makelist = make('list')
maxvalue = make('maxvalue')
mcoast = make('mcoast')
mcont = make('mcont')
mcross_sect = make('mcross_sect')
mgraph = make('mgraph')
mvertprofview = make('mvertprofview')
mxsectview = make('mxsectview')
met_plot = make('plot')
minvalue = make('minvalue')
mobs = make('mobs')
msymb = make('msymb')
mtext = make('mtext')
mvl_ml2hPa = make('mvl_ml2hPa')
netcdf_visuliser = make('netcdf_visuliser')
newpage = make('newpage')
obsfilter = make('obsfilter')
plot_page = make('plot_page')
plot_superpage = make('plot_superpage')
png_output = make('png_output')
power = make('^')
pr = make('print')
prod = make('*')
ps_output = make('ps_output')
read = make('read')
retrieve = make('retrieve')
second = make('second')
setcurrent = make('setcurrent')
_setoutput = make('setoutput')
sqrt = make('sqrt')
sub = make('-')
subset = make('[]')
type = make('type')
unique = make('unique')
value = make('value')
version_info = make('version_info')
waitmode = make('waitmode')
write = make('write')


class Plot():

    def __init__(self):
        self.plot_to_jupyter = False
        self.jupyter_available = False

    def __call__(self, *args, **kwargs):
        if self.plot_to_jupyter and self.jupyter_available:
            f, tmp = tempfile.mkstemp(".png")
            os.close(f)

            base, ext = os.path.splitext(tmp)

            _setoutput(png_output(output_name=base, output_name_first_page_number='off'))
            met_plot(*args)

            image = Image(tmp)
            os.unlink(tmp)
            return image
        else:
            map_outputs = {
                'png': png_output,
                'ps': ps_output,
            }
            if 'output_type' in kwargs:
                output_function = map_outputs[kwargs['output_type'].lower()]
                kwargs.pop('output_type')
                return met_plot(output_function(kwargs), *args)
            else:
                return met_plot(*args)


plot = Plot()


def setoutput(*args):
    if 'jupyter' in args:
        if plot.jupyter_available:
            plot.plot_to_jupyter = True
        else:
            print("setoutput('jupyter') was set, but we are not in a Jupyter environment")
    else:
        plot.plot_to_jupyter = False
        _setoutput(*args)


# try to import what we need to pass images back to Jupyter notebooks

try:
    from IPython.display import Image
    from IPython import get_ipython
    if get_ipython() is not None:
        plot.jupyter_available = True
        plot.plot_to_jupyter = True
except ImportError:
    pass

# perform a MARS retrieval
# - defined a request
# - set waitmode to 1 to force synchronisation
# - the return is a path to a temporary file, so copy it before end of script
# req = { 'PARAM' : 't',
#         'LEVELIST' : ['1000', '500'],
#         'GRID' : ['2', '2']}
# waitmode(1)
# g = retrieve(req)
# print(g)
# copyfile(g, './result.grib')
