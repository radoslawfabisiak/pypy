import os, py

from rpython.tool.udir import udir
from pypy.interpreter.gateway import unwrap_spec, interp2app
from pypy.module._cffi_backend.newtype import _clean_cache
import pypy.module.cpyext.api     # side-effect of pre-importing it


@unwrap_spec(cdef=str, module_name=str, source=str)
def prepare(space, cdef, module_name, source, w_includes=None,
            w_extra_source=None):
    try:
        import cffi
        from cffi import FFI            # <== the system one, which
        from cffi import recompiler     # needs to be at least cffi 1.0.4
        from cffi import ffiplatform
    except ImportError:
        py.test.skip("system cffi module not found or older than 1.0.0")
    if cffi.__version_info__ < (1, 0, 4):
        py.test.skip("system cffi module needs to be at least 1.0.4")
    space.appexec([], """():
        import _cffi_backend     # force it to be initialized
    """)
    includes = []
    if w_includes:
        includes += space.unpackiterable(w_includes)
    assert module_name.startswith('test_')
    module_name = '_CFFI_' + module_name
    rdir = udir.ensure('recompiler', dir=1)
    rdir.join('Python.h').write(
        '#define PYPY_VERSION XX\n'
        '#define PyMODINIT_FUNC /*exported*/ void\n'
        )
    path = module_name.replace('.', os.sep)
    if '.' in module_name:
        subrdir = rdir.join(module_name[:module_name.index('.')])
        os.mkdir(str(subrdir))
    else:
        subrdir = rdir
    c_file  = str(rdir.join('%s.c'  % path))
    ffi = FFI()
    for include_ffi_object in includes:
        ffi.include(include_ffi_object._test_recompiler_source_ffi)
    ffi.cdef(cdef)
    ffi.set_source(module_name, source)
    ffi.emit_c_code(c_file)

    base_module_name = module_name.split('.')[-1]
    sources = []
    if w_extra_source is not None:
        sources.append(space.str_w(w_extra_source))
    ext = ffiplatform.get_extension(c_file, module_name,
            include_dirs=[str(rdir)],
            export_symbols=['_cffi_pypyinit_' + base_module_name],
            sources=sources)
    ffiplatform.compile(str(rdir), ext)

    for extension in ['so', 'pyd', 'dylib']:
        so_file = str(rdir.join('%s.%s' % (path, extension)))
        if os.path.exists(so_file):
            break
    else:
        raise Exception("could not find the compiled extension module?")

    args_w = [space.wrap(module_name), space.wrap(so_file)]
    w_res = space.appexec(args_w, """(modulename, filename):
        import imp
        mod = imp.load_dynamic(modulename, filename)
        assert mod.__name__ == modulename
        return (mod.ffi, mod.lib)
    """)
    ffiobject = space.getitem(w_res, space.wrap(0))
    ffiobject._test_recompiler_source_ffi = ffi
    if not hasattr(space, '_cleanup_ffi'):
        space._cleanup_ffi = []
    space._cleanup_ffi.append(ffiobject)
    return w_res


class AppTestRecompiler:
    spaceconfig = dict(usemodules=['_cffi_backend', 'imp'])

    def setup_class(cls):
        if cls.runappdirect:
            py.test.skip("not a test for -A")
        cls.w_prepare = cls.space.wrap(interp2app(prepare))
        cls.w_udir = cls.space.wrap(str(udir))
        cls.w_os_sep = cls.space.wrap(os.sep)

    def setup_method(self, meth):
        self._w_modules = self.space.appexec([], """():
            import sys
            return set(sys.modules)
        """)

    def teardown_method(self, meth):
        if hasattr(self.space, '_cleanup_ffi'):
            for ffi in self.space._cleanup_ffi:
                del ffi.cached_types     # try to prevent cycles
            del self.space._cleanup_ffi
        self.space.appexec([self._w_modules], """(old_modules):
            import sys
            for key in sys.modules.keys():
                if key not in old_modules:
                    del sys.modules[key]
        """)
        _clean_cache(self.space)

    def test_math_sin(self):
        import math
        ffi, lib = self.prepare(
            "float sin(double); double cos(double);",
            'test_math_sin',
            '#include <math.h>')
        assert lib.cos(1.43) == math.cos(1.43)

    def test_repr_lib(self):
        ffi, lib = self.prepare(
            "",
            'test_repr_lib',
            "")
        assert repr(lib) == "<Lib object for '_CFFI_test_repr_lib'>"

    def test_funcarg_ptr(self):
        ffi, lib = self.prepare(
            "int foo(int *);",
            'test_funcarg_ptr',
            'int foo(int *p) { return *p; }')
        assert lib.foo([-12345]) == -12345

    def test_funcres_ptr(self):
        ffi, lib = self.prepare(
            "int *foo(void);",
            'test_funcres_ptr',
            'int *foo(void) { static int x=-12345; return &x; }')
        assert lib.foo()[0] == -12345

    def test_global_var_array(self):
        ffi, lib = self.prepare(
            "int a[100];",
            'test_global_var_array',
            'int a[100] = { 9999 };')
        lib.a[42] = 123456
        assert lib.a[42] == 123456
        assert lib.a[0] == 9999

    def test_verify_typedef(self):
        ffi, lib = self.prepare(
            "typedef int **foo_t;",
            'test_verify_typedef',
            'typedef int **foo_t;')
        assert ffi.sizeof("foo_t") == ffi.sizeof("void *")

    def test_verify_typedef_dotdotdot(self):
        ffi, lib = self.prepare(
            "typedef ... foo_t;",
            'test_verify_typedef_dotdotdot',
            'typedef int **foo_t;')
        # did not crash

    def test_verify_typedef_star_dotdotdot(self):
        ffi, lib = self.prepare(
            "typedef ... *foo_t;",
            'test_verify_typedef_star_dotdotdot',
            'typedef int **foo_t;')
        # did not crash

    def test_global_var_int(self):
        ffi, lib = self.prepare(
            "int a, b, c;",
            'test_global_var_int',
            'int a = 999, b, c;')
        assert lib.a == 999
        lib.a -= 1001
        assert lib.a == -2
        lib.a = -2147483648
        assert lib.a == -2147483648
        raises(OverflowError, "lib.a = 2147483648")
        raises(OverflowError, "lib.a = -2147483649")
        lib.b = 525      # try with the first access being in setattr, too
        assert lib.b == 525
        raises(AttributeError, "del lib.a")
        raises(AttributeError, "del lib.c")
        raises(AttributeError, "del lib.foobarbaz")

    def test_macro(self):
        ffi, lib = self.prepare(
            "#define FOOBAR ...",
            'test_macro',
            "#define FOOBAR (-6912)")
        assert lib.FOOBAR == -6912
        raises(AttributeError, "lib.FOOBAR = 2")

    def test_macro_check_value(self):
        # the value '-0x80000000' in C sources does not have a clear meaning
        # to me; it appears to have a different effect than '-2147483648'...
        # Moreover, on 32-bits, -2147483648 is actually equal to
        # -2147483648U, which in turn is equal to 2147483648U and so positive.
        import sys
        vals = ['42', '-42', '0x80000000', '-2147483648',
                '0', '9223372036854775809ULL',
                '-9223372036854775807LL']
        if sys.maxsize <= 2**32:
            vals.remove('-2147483648')

        cdef_lines = ['#define FOO_%d_%d %s' % (i, j, vals[i])
                      for i in range(len(vals))
                      for j in range(len(vals))]

        verify_lines = ['#define FOO_%d_%d %s' % (i, j, vals[j])  # [j], not [i]
                        for i in range(len(vals))
                        for j in range(len(vals))]

        ffi, lib = self.prepare(
            '\n'.join(cdef_lines),
            'test_macro_check_value_ok',
            '\n'.join(verify_lines))

        for j in range(len(vals)):
            c_got = int(vals[j].replace('U', '').replace('L', ''), 0)
            c_compiler_msg = str(c_got)
            if c_got > 0:
                c_compiler_msg += ' (0x%x)' % (c_got,)
            #
            for i in range(len(vals)):
                attrname = 'FOO_%d_%d' % (i, j)
                if i == j:
                    x = getattr(lib, attrname)
                    assert x == c_got
                else:
                    e = raises(ffi.error, getattr, lib, attrname)
                    assert str(e.value) == (
                        "the C compiler says '%s' is equal to "
                        "%s, but the cdef disagrees" % (attrname, c_compiler_msg))

    def test_constant(self):
        ffi, lib = self.prepare(
            "static const int FOOBAR;",
            'test_constant',
            "#define FOOBAR (-6912)")
        assert lib.FOOBAR == -6912
        raises(AttributeError, "lib.FOOBAR = 2")

    def test_check_value_of_static_const(self):
        ffi, lib = self.prepare(
            "static const int FOOBAR = 042;",
            'test_check_value_of_static_const',
            "#define FOOBAR (-6912)")
        e = raises(ffi.error, getattr, lib, 'FOOBAR')
        assert str(e.value) == (
           "the C compiler says 'FOOBAR' is equal to -6912, but the cdef disagrees")

    def test_constant_nonint(self):
        ffi, lib = self.prepare(
            "static const double FOOBAR;",
            'test_constant_nonint',
            "#define FOOBAR (-6912.5)")
        assert lib.FOOBAR == -6912.5
        raises(AttributeError, "lib.FOOBAR = 2")

    def test_constant_ptr(self):
        ffi, lib = self.prepare(
            "static double *const FOOBAR;",
            'test_constant_ptr',
            "#define FOOBAR NULL")
        assert lib.FOOBAR == ffi.NULL
        assert ffi.typeof(lib.FOOBAR) == ffi.typeof("double *")

    def test_dir(self):
        ffi, lib = self.prepare(
            "int ff(int); int aa; static const int my_constant;",
            'test_dir', """
            #define my_constant  (-45)
            int aa;
            int ff(int x) { return x+aa; }
        """)
        lib.aa = 5
        assert dir(lib) == ['aa', 'ff', 'my_constant']

    def test_verify_opaque_struct(self):
        ffi, lib = self.prepare(
            "struct foo_s;",
            'test_verify_opaque_struct',
            "struct foo_s;")
        assert ffi.typeof("struct foo_s").cname == "struct foo_s"

    def test_verify_opaque_union(self):
        ffi, lib = self.prepare(
            "union foo_s;",
            'test_verify_opaque_union',
            "union foo_s;")
        assert ffi.typeof("union foo_s").cname == "union foo_s"

    def test_verify_struct(self):
        ffi, lib = self.prepare(
            """struct foo_s { int b; short a; ...; };
               struct bar_s { struct foo_s *f; };""",
            'test_verify_struct',
            """struct foo_s { short a; int b; };
               struct bar_s { struct foo_s *f; };""")
        ffi.typeof("struct bar_s *")
        p = ffi.new("struct foo_s *", {'a': -32768, 'b': -2147483648})
        assert p.a == -32768
        assert p.b == -2147483648
        raises(OverflowError, "p.a -= 1")
        raises(OverflowError, "p.b -= 1")
        q = ffi.new("struct bar_s *", {'f': p})
        assert q.f == p
        #
        assert ffi.offsetof("struct foo_s", "a") == 0
        assert ffi.offsetof("struct foo_s", "b") == 4
        assert ffi.offsetof(u"struct foo_s", u"b") == 4
        #
        raises(TypeError, ffi.addressof, p)
        assert ffi.addressof(p[0]) == p
        assert ffi.typeof(ffi.addressof(p[0])) is ffi.typeof("struct foo_s *")
        assert ffi.typeof(ffi.addressof(p, "b")) is ffi.typeof("int *")
        assert ffi.addressof(p, "b")[0] == p.b

    def test_verify_exact_field_offset(self):
        ffi, lib = self.prepare(
            """struct foo_s { int b; short a; };""",
            'test_verify_exact_field_offset',
            """struct foo_s { short a; int b; };""")
        e = raises(ffi.error, ffi.new, "struct foo_s *", [])    # lazily
        assert str(e.value) == ("struct foo_s: wrong offset for field 'b' (cdef "
                           'says 0, but C compiler says 4). fix it or use "...;" '
                           "in the cdef for struct foo_s to make it flexible")

    def test_type_caching(self):
        ffi1, lib1 = self.prepare(
            "struct foo_s;",
            'test_type_caching_1',
            'struct foo_s;')
        ffi2, lib2 = self.prepare(
            "struct foo_s;",    # different one!
            'test_type_caching_2',
            'struct foo_s;')
        # shared types
        assert ffi1.typeof("long") is ffi2.typeof("long")
        assert ffi1.typeof("long**") is ffi2.typeof("long * *")
        assert ffi1.typeof("long(*)(int, ...)") is ffi2.typeof("long(*)(int, ...)")
        # non-shared types
        assert ffi1.typeof("struct foo_s") is not ffi2.typeof("struct foo_s")
        assert ffi1.typeof("struct foo_s *") is not ffi2.typeof("struct foo_s *")
        assert ffi1.typeof("struct foo_s*(*)()") is not (
            ffi2.typeof("struct foo_s*(*)()"))
        assert ffi1.typeof("void(*)(struct foo_s*)") is not (
            ffi2.typeof("void(*)(struct foo_s*)"))

    def test_verify_enum(self):
        import sys
        ffi, lib = self.prepare(
            """enum e1 { B1, A1, ... }; enum e2 { B2, A2, ... };""",
            'test_verify_enum',
            "enum e1 { A1, B1, C1=%d };" % sys.maxsize +
            "enum e2 { A2, B2, C2 };")
        ffi.typeof("enum e1")
        ffi.typeof("enum e2")
        assert lib.A1 == 0
        assert lib.B1 == 1
        assert lib.A2 == 0
        assert lib.B2 == 1
        assert ffi.sizeof("enum e1") == ffi.sizeof("long")
        assert ffi.sizeof("enum e2") == ffi.sizeof("int")
        assert repr(ffi.cast("enum e1", 0)) == "<cdata 'enum e1' 0: A1>"

    def test_dotdotdot_length_of_array_field(self):
        ffi, lib = self.prepare(
            "struct foo_s { int a[...]; int b[...]; };",
            'test_dotdotdot_length_of_array_field',
            "struct foo_s { int a[42]; int b[11]; };")
        assert ffi.sizeof("struct foo_s") == (42 + 11) * 4
        p = ffi.new("struct foo_s *")
        assert p.a[41] == p.b[10] == 0
        raises(IndexError, "p.a[42]")
        raises(IndexError, "p.b[11]")

    def test_dotdotdot_global_array(self):
        ffi, lib = self.prepare(
            "int aa[...]; int bb[...];",
            'test_dotdotdot_global_array',
            "int aa[41]; int bb[12];")
        assert ffi.sizeof(lib.aa) == 41 * 4
        assert ffi.sizeof(lib.bb) == 12 * 4
        assert lib.aa[40] == lib.bb[11] == 0
        raises(IndexError, "lib.aa[41]")
        raises(IndexError, "lib.bb[12]")

    def test_misdeclared_field_1(self):
        ffi, lib = self.prepare(
            "struct foo_s { int a[5]; };",
            'test_misdeclared_field_1',
            "struct foo_s { int a[6]; };")
        assert ffi.sizeof("struct foo_s") == 24  # found by the actual C code
        p = ffi.new("struct foo_s *")
        # lazily build the fields and boom:
        e = raises(ffi.error, getattr, p, "a")
        assert str(e.value).startswith("struct foo_s: wrong size for field 'a' "
                                       "(cdef says 20, but C compiler says 24)")

    def test_open_array_in_struct(self):
        ffi, lib = self.prepare(
            "struct foo_s { int b; int a[]; };",
            'test_open_array_in_struct',
            "struct foo_s { int b; int a[]; };")
        assert ffi.sizeof("struct foo_s") == 4
        p = ffi.new("struct foo_s *", [5, [10, 20, 30]])
        assert p.a[2] == 30

    def test_math_sin_type(self):
        ffi, lib = self.prepare(
            "double sin(double);",
            'test_math_sin_type',
            '#include <math.h>')
        # 'lib.sin' is typed as a <built-in method> object on lib
        assert ffi.typeof(lib.sin).cname == "double(*)(double)"
        # 'x' is another <built-in method> object on lib, made very indirectly
        x = type(lib).__dir__.__get__(lib)
        raises(TypeError, ffi.typeof, x)

    def test_verify_anonymous_struct_with_typedef(self):
        ffi, lib = self.prepare(
            "typedef struct { int a; long b; ...; } foo_t;",
            'test_verify_anonymous_struct_with_typedef',
            "typedef struct { long b; int hidden, a; } foo_t;")
        p = ffi.new("foo_t *", {'b': 42})
        assert p.b == 42
        assert repr(p).startswith("<cdata 'foo_t *' ")

    def test_verify_anonymous_struct_with_star_typedef(self):
        ffi, lib = self.prepare(
            "typedef struct { int a; long b; } *foo_t;",
            'test_verify_anonymous_struct_with_star_typedef',
            "typedef struct { int a; long b; } *foo_t;")
        p = ffi.new("foo_t", {'b': 42})
        assert p.b == 42

    def test_verify_anonymous_enum_with_typedef(self):
        ffi, lib = self.prepare(
            "typedef enum { AA, ... } e1;",
            'test_verify_anonymous_enum_with_typedef1',
            "typedef enum { BB, CC, AA } e1;")
        assert lib.AA == 2
        assert ffi.sizeof("e1") == ffi.sizeof("int")
        assert repr(ffi.cast("e1", 2)) == "<cdata 'e1' 2: AA>"
        #
        import sys
        ffi, lib = self.prepare(
            "typedef enum { AA=%d } e1;" % sys.maxsize,
            'test_verify_anonymous_enum_with_typedef2',
            "typedef enum { AA=%d } e1;" % sys.maxsize)
        assert lib.AA == sys.maxsize
        assert ffi.sizeof("e1") == ffi.sizeof("long")

    def test_unique_types(self):
        CDEF = "struct foo_s; union foo_u; enum foo_e { AA };"
        ffi1, lib1 = self.prepare(CDEF, "test_unique_types_1", CDEF)
        ffi2, lib2 = self.prepare(CDEF, "test_unique_types_2", CDEF)
        #
        assert ffi1.typeof("char") is ffi2.typeof("char ")
        assert ffi1.typeof("long") is ffi2.typeof("signed long int")
        assert ffi1.typeof("double *") is ffi2.typeof("double*")
        assert ffi1.typeof("int ***") is ffi2.typeof(" int * * *")
        assert ffi1.typeof("int[]") is ffi2.typeof("signed int[]")
        assert ffi1.typeof("signed int*[17]") is ffi2.typeof("int *[17]")
        assert ffi1.typeof("void") is ffi2.typeof("void")
        assert ffi1.typeof("int(*)(int,int)") is ffi2.typeof("int(*)(int,int)")
        #
        # these depend on user-defined data, so should not be shared
        for name in ["struct foo_s",
                     "union foo_u *",
                     "enum foo_e",
                     "struct foo_s *(*)()",
                     "void(*)(struct foo_s *)",
                     "struct foo_s *(*[5])[8]",
                     ]:
            assert ffi1.typeof(name) is not ffi2.typeof(name)
        # sanity check: twice 'ffi1'
        assert ffi1.typeof("struct foo_s*") is ffi1.typeof("struct foo_s *")

    def test_module_name_in_package(self):
        ffi, lib = self.prepare(
            "int foo(int);",
            'test_module_name_in_package.mymod',
            "int foo(int x) { return x + 32; }")
        assert lib.foo(10) == 42

    def test_bad_size_of_global_1(self):
        ffi, lib = self.prepare(
            "short glob;",
            "test_bad_size_of_global_1",
            "long glob;")
        raises(ffi.error, getattr, lib, "glob")

    def test_bad_size_of_global_2(self):
        ffi, lib = self.prepare(
            "int glob[10];",
            "test_bad_size_of_global_2",
            "int glob[9];")
        e = raises(ffi.error, getattr, lib, "glob")
        assert str(e.value) == ("global variable 'glob' should be 40 bytes "
                                "according to the cdef, but is actually 36")

    def test_unspecified_size_of_global(self):
        ffi, lib = self.prepare(
            "int glob[];",
            "test_unspecified_size_of_global",
            "int glob[10];")
        lib.glob    # does not crash

    def test_include_1(self):
        ffi1, lib1 = self.prepare(
            "typedef double foo_t;",
            "test_include_1_parent",
            "typedef double foo_t;")
        ffi, lib = self.prepare(
            "foo_t ff1(foo_t);",
            "test_include_1",
            "double ff1(double x) { return 42.5; }",
            includes=[ffi1])
        assert lib.ff1(0) == 42.5
        assert ffi1.typeof("foo_t") is ffi.typeof("foo_t") \
            is ffi.typeof("double")

    def test_include_1b(self):
        ffi1, lib1 = self.prepare(
            "int foo1(int);",
            "test_include_1b_parent",
            "int foo1(int x) { return x + 10; }")
        ffi, lib = self.prepare(
            "int foo2(int);",
            "test_include_1b",
            "int foo2(int x) { return x - 5; }",
            includes=[ffi1])
        assert lib.foo2(42) == 37
        assert lib.foo1(42) == 52
        assert lib.foo1 is lib1.foo1

    def test_include_2(self):
        ffi1, lib1 = self.prepare(
            "struct foo_s { int x, y; };",
            "test_include_2_parent",
            "struct foo_s { int x, y; };")
        ffi, lib = self.prepare(
            "struct foo_s *ff2(struct foo_s *);",
            "test_include_2",
            "struct foo_s { int x, y; }; //usually from a #include\n"
            "struct foo_s *ff2(struct foo_s *p) { p->y++; return p; }",
            includes=[ffi1])
        p = ffi.new("struct foo_s *")
        p.y = 41
        q = lib.ff2(p)
        assert q == p
        assert p.y == 42
        assert ffi1.typeof("struct foo_s") is ffi.typeof("struct foo_s")

    def test_include_3(self):
        ffi1, lib1 = self.prepare(
            "typedef short sshort_t;",
            "test_include_3_parent",
            "typedef short sshort_t;")
        ffi, lib = self.prepare(
            "sshort_t ff3(sshort_t);",
            "test_include_3",
            "typedef short sshort_t; //usually from a #include\n"
            "sshort_t ff3(sshort_t x) { return x + 42; }",
            includes=[ffi1])
        assert lib.ff3(10) == 52
        assert ffi.typeof(ffi.cast("sshort_t", 42)) is ffi.typeof("short")
        assert ffi1.typeof("sshort_t") is ffi.typeof("sshort_t")

    def test_include_4(self):
        ffi1, lib1 = self.prepare(
            "typedef struct { int x; } mystruct_t;",
            "test_include_4_parent",
            "typedef struct { int x; } mystruct_t;")
        ffi, lib = self.prepare(
            "mystruct_t *ff4(mystruct_t *);",
            "test_include_4",
            "typedef struct {int x; } mystruct_t; //usually from a #include\n"
            "mystruct_t *ff4(mystruct_t *p) { p->x += 42; return p; }",
            includes=[ffi1])
        p = ffi.new("mystruct_t *", [10])
        q = lib.ff4(p)
        assert q == p
        assert p.x == 52
        assert ffi1.typeof("mystruct_t") is ffi.typeof("mystruct_t")

    def test_include_5(self):
        ffi1, lib1 = self.prepare(
            "typedef struct { int x[2]; int y; } *mystruct_p;",
            "test_include_5_parent",
            "typedef struct { int x[2]; int y; } *mystruct_p;")
        ffi, lib = self.prepare(
            "mystruct_p ff5(mystruct_p);",
            "test_include_5",
            "typedef struct {int x[2]; int y; } *mystruct_p; //#include\n"
            "mystruct_p ff5(mystruct_p p) { p->x[1] += 42; return p; }",
            includes=[ffi1])
        assert ffi.alignof(ffi.typeof("mystruct_p").item) == 4
        assert ffi1.typeof("mystruct_p") is ffi.typeof("mystruct_p")
        p = ffi.new("mystruct_p", [[5, 10], -17])
        q = lib.ff5(p)
        assert q == p
        assert p.x[0] == 5
        assert p.x[1] == 52
        assert p.y == -17
        assert ffi.alignof(ffi.typeof(p[0])) == 4

    def test_include_6(self):
        ffi1, lib1 = self.prepare(
            "typedef ... mystruct_t;",
            "test_include_6_parent",
            "typedef struct _mystruct_s mystruct_t;")
        ffi, lib = self.prepare(
            "mystruct_t *ff6(void); int ff6b(mystruct_t *);",
            "test_include_6",
           "typedef struct _mystruct_s mystruct_t; //usually from a #include\n"
           "struct _mystruct_s { int x; };\n"
           "static mystruct_t result_struct = { 42 };\n"
           "mystruct_t *ff6(void) { return &result_struct; }\n"
           "int ff6b(mystruct_t *p) { return p->x; }",
           includes=[ffi1])
        p = lib.ff6()
        assert ffi.cast("int *", p)[0] == 42
        assert lib.ff6b(p) == 42

    def test_include_7(self):
        ffi1, lib1 = self.prepare(
            "typedef ... mystruct_t; int ff7b(mystruct_t *);",
            "test_include_7_parent",
           "typedef struct { int x; } mystruct_t;\n"
           "int ff7b(mystruct_t *p) { return p->x; }")
        ffi, lib = self.prepare(
            "mystruct_t *ff7(void);",
            "test_include_7",
           "typedef struct { int x; } mystruct_t; //usually from a #include\n"
           "static mystruct_t result_struct = { 42 };"
           "mystruct_t *ff7(void) { return &result_struct; }",
           includes=[ffi1])
        p = lib.ff7()
        assert ffi.cast("int *", p)[0] == 42
        assert lib.ff7b(p) == 42

    def test_include_8(self):
        ffi1, lib1 = self.prepare(
            "struct foo_s;",
            "test_include_8_parent",
            "struct foo_s;")
        ffi, lib = self.prepare(
            "struct foo_s { int x, y; };",
            "test_include_8",
            "struct foo_s { int x, y; };",
            includes=[ffi1])
        e = raises(NotImplementedError, ffi.new, "struct foo_s *")
        assert str(e.value) == (
            "'struct foo_s' is opaque in the ffi.include(), but no longer in "
            "the ffi doing the include (workaround: don't use ffi.include() but"
            " duplicate the declarations of everything using struct foo_s)")

    def test_bitfield_basic(self):
        ffi, lib = self.prepare(
            "struct bitfield { int a:10, b:25; };",
            "test_bitfield_basic",
            "struct bitfield { int a:10, b:25; };")
        assert ffi.sizeof("struct bitfield") == 8
        s = ffi.new("struct bitfield *")
        s.a = -512
        raises(OverflowError, "s.a = -513")
        assert s.a == -512

    def test_incomplete_struct_as_arg(self):
        ffi, lib = self.prepare(
            "struct foo_s { int x; ...; }; int f(int, struct foo_s);",
            "test_incomplete_struct_as_arg",
            "struct foo_s { int a, x, z; };\n"
            "int f(int b, struct foo_s s) { return s.x * b; }")
        s = ffi.new("struct foo_s *", [21])
        assert s.x == 21
        assert ffi.sizeof(s[0]) == 12
        assert ffi.offsetof(ffi.typeof(s), 'x') == 4
        assert lib.f(2, s[0]) == 42
        assert ffi.typeof(lib.f) == ffi.typeof("int(*)(int, struct foo_s)")

    def test_incomplete_struct_as_result(self):
        ffi, lib = self.prepare(
            "struct foo_s { int x; ...; }; struct foo_s f(int);",
            "test_incomplete_struct_as_result",
            "struct foo_s { int a, x, z; };\n"
            "struct foo_s f(int x) { struct foo_s r; r.x = x * 2; return r; }")
        s = lib.f(21)
        assert s.x == 42
        assert ffi.typeof(lib.f) == ffi.typeof("struct foo_s(*)(int)")

    def test_incomplete_struct_as_both(self):
        ffi, lib = self.prepare(
            "struct foo_s { int x; ...; }; struct bar_s { int y; ...; };\n"
            "struct foo_s f(int, struct bar_s);",
            "test_incomplete_struct_as_both",
            "struct foo_s { int a, x, z; };\n"
            "struct bar_s { int b, c, y, d; };\n"
            "struct foo_s f(int x, struct bar_s b) {\n"
            "  struct foo_s r; r.x = x * b.y; return r;\n"
            "}")
        b = ffi.new("struct bar_s *", [7])
        s = lib.f(6, b[0])
        assert s.x == 42
        assert ffi.typeof(lib.f) == ffi.typeof(
            "struct foo_s(*)(int, struct bar_s)")
        s = lib.f(14, {'y': -3})
        assert s.x == -42

    def test_name_of_unnamed_struct(self):
        ffi, lib = self.prepare(
                 "typedef struct { int x; } foo_t;\n"
                 "typedef struct { int y; } *bar_p;\n"
                 "typedef struct { int y; } **baz_pp;\n",
                 "test_name_of_unnamed_struct",
                 "typedef struct { int x; } foo_t;\n"
                 "typedef struct { int y; } *bar_p;\n"
                 "typedef struct { int y; } **baz_pp;\n")
        assert repr(ffi.typeof("foo_t")) == "<ctype 'foo_t'>"
        assert repr(ffi.typeof("bar_p")) == "<ctype 'struct $1 *'>"
        assert repr(ffi.typeof("baz_pp")) == "<ctype 'struct $2 * *'>"

    def test_address_of_global_var(self):
        ffi, lib = self.prepare("""
            long bottom, bottoms[2];
            long FetchRectBottom(void);
            long FetchRectBottoms1(void);
            #define FOOBAR 42
        """, "test_address_of_global_var", """
            long bottom, bottoms[2];
            long FetchRectBottom(void) { return bottom; }
            long FetchRectBottoms1(void) { return bottoms[1]; }
            #define FOOBAR 42
        """)
        lib.bottom = 300
        assert lib.FetchRectBottom() == 300
        lib.bottom += 1
        assert lib.FetchRectBottom() == 301
        lib.bottoms[1] = 500
        assert lib.FetchRectBottoms1() == 500
        lib.bottoms[1] += 2
        assert lib.FetchRectBottoms1() == 502
        #
        p = ffi.addressof(lib, 'bottom')
        assert ffi.typeof(p) == ffi.typeof("long *")
        assert p[0] == 301
        p[0] += 1
        assert lib.FetchRectBottom() == 302
        p = ffi.addressof(lib, 'bottoms')
        assert ffi.typeof(p) == ffi.typeof("long(*)[2]")
        assert p[0] == lib.bottoms
        #
        raises(AttributeError, ffi.addressof, lib, 'unknown_var')
        raises(AttributeError, ffi.addressof, lib, "FOOBAR")

    def test_defines__CFFI_(self):
        # Check that we define the macro _CFFI_ automatically.
        # It should be done before including Python.h, so that PyPy's Python.h
        # can check for it.
        ffi, lib = self.prepare("""
            #define CORRECT 1
        """, "test_defines__CFFI_", """
            #ifdef _CFFI_
            #    define CORRECT 1
            #endif
        """)
        assert lib.CORRECT == 1

    def test_unpack_args(self):
        ffi, lib = self.prepare(
            "void foo0(void); void foo1(int); void foo2(int, int);",
            "test_unpack_args", """
                void foo0(void) { }
                void foo1(int x) { }
                void foo2(int x, int y) { }
            """)
        assert 'foo0' in repr(lib.foo0)
        assert 'foo1' in repr(lib.foo1)
        assert 'foo2' in repr(lib.foo2)
        lib.foo0()
        lib.foo1(42)
        lib.foo2(43, 44)
        e1 = raises(TypeError, lib.foo0, 42)
        e2 = raises(TypeError, lib.foo0, 43, 44)
        e3 = raises(TypeError, lib.foo1)
        e4 = raises(TypeError, lib.foo1, 43, 44)
        e5 = raises(TypeError, lib.foo2)
        e6 = raises(TypeError, lib.foo2, 42)
        e7 = raises(TypeError, lib.foo2, 45, 46, 47)
        assert str(e1.value) == "foo0() takes no arguments (1 given)"
        assert str(e2.value) == "foo0() takes no arguments (2 given)"
        assert str(e3.value) == "foo1() takes exactly one argument (0 given)"
        assert str(e4.value) == "foo1() takes exactly one argument (2 given)"
        assert str(e5.value) == "foo2() takes exactly 2 arguments (0 given)"
        assert str(e6.value) == "foo2() takes exactly 2 arguments (1 given)"
        assert str(e7.value) == "foo2() takes exactly 2 arguments (3 given)"

    def test_address_of_function(self):
        ffi, lib = self.prepare(
            "long myfunc(long x);",
            "test_addressof_function",
            "char myfunc(char x) { return (char)(x + 42); }")
        assert lib.myfunc(5) == 47
        assert lib.myfunc(0xABC05) == 47
        assert not isinstance(lib.myfunc, ffi.CData)
        assert ffi.typeof(lib.myfunc) == ffi.typeof("long(*)(long)")
        addr = ffi.addressof(lib, 'myfunc')
        assert addr(5) == 47
        assert addr(0xABC05) == 47
        assert isinstance(addr, ffi.CData)
        assert ffi.typeof(addr) == ffi.typeof("long(*)(long)")

    def test_issue198(self):
        ffi, lib = self.prepare("""
            typedef struct{...;} opaque_t;
            const opaque_t CONSTANT;
            int toint(opaque_t);
        """, 'test_issue198', """
            typedef int opaque_t;
            #define CONSTANT ((opaque_t)42)
            static int toint(opaque_t o) { return o; }
        """)
        def random_stuff():
            pass
        assert lib.toint(lib.CONSTANT) == 42
        random_stuff()
        assert lib.toint(lib.CONSTANT) == 42

    def test_constant_is_not_a_compiler_constant(self):
        ffi, lib = self.prepare(
            "static const float almost_forty_two;",
            'test_constant_is_not_a_compiler_constant', """
                static float f(void) { return 42.25; }
                #define almost_forty_two (f())
            """)
        assert lib.almost_forty_two == 42.25

    def test_variable_of_unknown_size(self):
        ffi, lib = self.prepare("""
            typedef ... opaque_t;
            opaque_t globvar;
        """, 'test_constant_of_unknown_size', """
            typedef char opaque_t[6];
            opaque_t globvar = "hello";
        """)
        # can't read or write it at all
        e = raises(TypeError, getattr, lib, 'globvar')
        assert str(e.value) == "'opaque_t' is opaque or not completed yet"
        e = raises(TypeError, setattr, lib, 'globvar', [])
        assert str(e.value) == "'opaque_t' is opaque or not completed yet"
        # but we can get its address
        p = ffi.addressof(lib, 'globvar')
        assert ffi.typeof(p) == ffi.typeof('opaque_t *')
        assert ffi.string(ffi.cast("char *", p), 8) == "hello"

    def test_constant_of_value_unknown_to_the_compiler(self):
        extra_c_source = self.udir + self.os_sep + (
            'extra_test_constant_of_value_unknown_to_the_compiler.c')
        with open(extra_c_source, 'w') as f:
            f.write('const int external_foo = 42;\n')
        ffi, lib = self.prepare(
            "const int external_foo;",
            'test_constant_of_value_unknown_to_the_compiler',
            "extern const int external_foo;",
            extra_source=extra_c_source)
        assert lib.external_foo == 42

    def test_call_with_incomplete_structs(self):
        ffi, lib = self.prepare(
            "typedef struct {...;} foo_t; "
            "foo_t myglob; "
            "foo_t increment(foo_t s); "
            "double getx(foo_t s);",
            'test_call_with_incomplete_structs', """
            typedef double foo_t;
            double myglob = 42.5;
            double getx(double x) { return x; }
            double increment(double x) { return x + 1; }
        """)
        assert lib.getx(lib.myglob) == 42.5
        assert lib.getx(lib.increment(lib.myglob)) == 43.5

    def test_struct_array_guess_length_2(self):
        ffi, lib = self.prepare(
            "struct foo_s { int a[...][...]; };",
            'test_struct_array_guess_length_2',
            "struct foo_s { int x; int a[5][8]; int y; };")
        assert ffi.sizeof('struct foo_s') == 42 * ffi.sizeof('int')
        s = ffi.new("struct foo_s *")
        assert ffi.sizeof(s.a) == 40 * ffi.sizeof('int')
        assert s.a[4][7] == 0
        raises(IndexError, 's.a[4][8]')
        raises(IndexError, 's.a[5][0]')
        assert ffi.typeof(s.a) == ffi.typeof("int[5][8]")
        assert ffi.typeof(s.a[0]) == ffi.typeof("int[8]")

    def test_global_var_array_2(self):
        ffi, lib = self.prepare(
            "int a[...][...];",
            'test_global_var_array_2',
            'int a[10][8];')
        lib.a[9][7] = 123456
        assert lib.a[9][7] == 123456
        raises(IndexError, 'lib.a[0][8]')
        raises(IndexError, 'lib.a[10][0]')
        assert ffi.typeof(lib.a) == ffi.typeof("int[10][8]")
        assert ffi.typeof(lib.a[0]) == ffi.typeof("int[8]")

    def test_some_integer_type(self):
        ffi, lib = self.prepare("""
            typedef int... foo_t;
            typedef unsigned long... bar_t;
            typedef struct { foo_t a, b; } mystruct_t;
            foo_t foobar(bar_t, mystruct_t);
            static const bar_t mu = -20;
            static const foo_t nu = 20;
        """, 'test_some_integer_type', """
            typedef unsigned long long foo_t;
            typedef short bar_t;
            typedef struct { foo_t a, b; } mystruct_t;
            static foo_t foobar(bar_t x, mystruct_t s) {
                return (foo_t)x + s.a + s.b;
            }
            static const bar_t mu = -20;
            static const foo_t nu = 20;
        """)
        assert ffi.sizeof("foo_t") == ffi.sizeof("unsigned long long")
        assert ffi.sizeof("bar_t") == ffi.sizeof("short")
        maxulonglong = 2 ** 64 - 1
        assert int(ffi.cast("foo_t", -1)) == maxulonglong
        assert int(ffi.cast("bar_t", -1)) == -1
        assert lib.foobar(-1, [0, 0]) == maxulonglong
        assert lib.foobar(2 ** 15 - 1, [0, 0]) == 2 ** 15 - 1
        assert lib.foobar(10, [20, 31]) == 61
        assert lib.foobar(0, [0, maxulonglong]) == maxulonglong
        raises(OverflowError, lib.foobar, 2 ** 15, [0, 0])
        raises(OverflowError, lib.foobar, -(2 ** 15) - 1, [0, 0])
        raises(OverflowError, ffi.new, "mystruct_t *", [0, -1])
        assert lib.mu == -20
        assert lib.nu == 20

    def test_issue200(self):
        ffi, lib = self.prepare("""
            typedef void (function_t)(void*);
            void function(void *);
        """, 'test_issue200', """
            static void function(void *p) { (void)p; }
        """)
        ffi.typeof('function_t*')
        lib.function(ffi.NULL)
        # assert did not crash

    def test_alignment_of_longlong(self):
        import _cffi_backend
        BULongLong = _cffi_backend.new_primitive_type('unsigned long long')
        x1 = _cffi_backend.alignof(BULongLong)
        assert x1 in [4, 8]
        #
        ffi, lib = self.prepare(
            "struct foo_s { unsigned long long x; };",
            'test_alignment_of_longlong',
            "struct foo_s { unsigned long long x; };")
        assert ffi.alignof('unsigned long long') == x1
        assert ffi.alignof('struct foo_s') == x1
