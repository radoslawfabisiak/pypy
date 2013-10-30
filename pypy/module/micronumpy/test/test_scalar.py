from pypy.module.micronumpy.test.test_base import BaseNumpyAppTest

class AppTestScalar(BaseNumpyAppTest):
    spaceconfig = dict(usemodules=["micronumpy", "binascii", "struct"])

    def test_init(self):
        import numpypy as np
        import math
        assert np.intp() == np.intp(0)
        assert np.intp('123') == np.intp(123)
        raises(TypeError, np.intp, None)
        assert np.float64() == np.float64(0)
        assert math.isnan(np.float64(None))
        assert np.bool_() == np.bool_(False)
        assert np.bool_('abc') == np.bool_(True)
        assert np.bool_(None) == np.bool_(False)
        assert np.complex_() == np.complex_(0)
        #raises(TypeError, np.complex_, '1+2j')
        assert math.isnan(np.complex_(None))

    def test_pickle(self):
        from numpypy import dtype, int32, float64, complex128, zeros, sum
        from numpypy.core.multiarray import scalar
        from cPickle import loads, dumps
        i = int32(1337)
        f = float64(13.37)
        c = complex128(13 + 37.j)

        assert i.__reduce__() == (scalar, (dtype('int32'), '9\x05\x00\x00'))
        assert f.__reduce__() == (scalar, (dtype('float64'), '=\n\xd7\xa3p\xbd*@'))
        assert c.__reduce__() == (scalar, (dtype('complex128'), '\x00\x00\x00\x00\x00\x00*@\x00\x00\x00\x00\x00\x80B@'))

        assert loads(dumps(i)) == i
        assert loads(dumps(f)) == f
        assert loads(dumps(c)) == c

        a = zeros(3)
        assert loads(dumps(sum(a))) == sum(a)

    def test_round(self):
        from numpypy import int32, float64, complex128, bool_
        i = int32(1337)
        f = float64(13.37)
        c = complex128(13 + 37.j)
        b = bool_(1)
        assert i.round(decimals=-2) == 1300
        assert i.round(decimals=1) == 1337
        assert c.round() == c
        assert f.round() == 13.
        assert f.round(decimals=-1) == 10.
        assert f.round(decimals=1) == 13.4
        assert b.round() == 1.0
        assert b.round(decimals=5) is b

    def test_attributes(self):
        import numpypy as np
        assert np.int64(0).dtype == np.dtype('int64')
        assert np.int64(0).itemsize == 8
