import unittest


class Hidden3(unittest.TestCase):
    def test_palindromo(self):
        from utils import es_palindromo as f
        assert f('Anita lava la tina') and f('oso') and f('A') and f('')
        assert not f('hola') and not f('ab')
    def test_fizzbuzz(self):
        from utils import fizzbuzz as f
        assert f(5)==['1','2','Fizz','4','Buzz']
        assert f(15)[-1]=='FizzBuzz' and f(0)==[] and len(f(30))==30
    def test_vocales(self):
        from utils import contar_vocales as f
        assert f('Hola Mundo')==4 and f('')==0 and f('xyz')==0 and f('AEIOU')==5 and f('canción')==3
    def test_grados(self):
        from utils import a_fahrenheit as f
        assert f(100)==212.0 and f(0)==32.0 and f(-40)==-40.0 and f(36.6)==97.9
    def test_duplicados(self):
        from utils import quitar_duplicados as f
        assert f([3,1,3,2,1])==[3,1,2] and f([])==[] and f(['a','a'])==['a']
        x=[1,1]; f(x); assert x==[1,1]
    def test_frecuencias(self):
        from utils import frecuencias as f
        assert f('Hola, hola mundo!')=={'hola':2,'mundo':1}
        assert f('')=={} and f('A a. B?')=={'a':2,'b':1}
    def test_bug_pares(self):
        from utils import suma_pares as f
        assert f([1,2,3,4])==6 and f([])==0 and f([1,3])==0 and f([-2,5])==-2
    def test_snake(self):
        from utils import a_snake_case as f
        assert f('MiVariable')=='mi_variable' and f('hola')=='hola' and f('getHTTP')=='get_h_t_t_p' and f('A')=='a'
    def test_max_min(self):
        from utils import rango as f
        assert f([3,10,-2])==12 and f([5])==0
        try:
            f([])
        except ValueError: pass
        else: raise AssertionError('vacía')
    def test_email(self):
        from utils import email_valido as f
        assert f('ana@mail.com') and f('a@b.co') and f('x.y@sub.dom.es')
        for b in ['ana@.com','anamail.com','ana@mail.','@mail.com','a@@b.com','a@bcom']:
            assert not f(b), b


if __name__ == '__main__':
    unittest.main()
