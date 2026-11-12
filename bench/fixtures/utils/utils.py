"""Utilidades del proyecto. Implementa cada función según su docstring."""

def es_palindromo(texto):
    """Devuelve True si el texto se lee igual al revés, ignorando mayúsculas y espacios. Ejemplos:
    es_palindromo('Anita lava la tina') -> True; es_palindromo('hola') -> False."""
    raise NotImplementedError

def fizzbuzz(n):
    """Función que devuelve la lista de 1 a n (incluido): 'Fizz' si es múltiplo de 3, 'Buzz' si es múltiplo de 5,
    'FizzBuzz' si de ambos, y si no el número como texto. Ejemplo: fizzbuzz(5) ->
    ['1','2','Fizz','4','Buzz']."""
    raise NotImplementedError

def contar_vocales(texto):
    """Función que cuenta las vocales a, e, i, o, u (sin distinguir mayúsculas; las vocales con tilde á é í ó ú
    también cuentan). Ejemplo: contar_vocales('Hola Mundo') -> 4."""
    raise NotImplementedError

def a_fahrenheit(celsius):
    """Convierte grados Celsius a Fahrenheit con la fórmula F = C * 9 / 5 + 32, redondeado a 1 decimal. Ejemplo: a_fahrenheit(100) -> 212.0;
    a_fahrenheit(36.6) -> 97.9."""
    raise NotImplementedError

def quitar_duplicados(lista):
    """Función que devuelve una lista nueva sin elementos repetidos, conservando el orden de la primera aparición.
    Ejemplo: quitar_duplicados([3,1,3,2,1]) -> [3,1,2]."""
    raise NotImplementedError

def frecuencias(texto):
    """Función que cuenta cuántas veces aparece cada palabra. Pasa todo a minúsculas, quita los signos . , ! ? y
    separa por espacios. Ejemplo: frecuencias('Hola, hola mundo!') -> {'hola': 2, 'mundo': 1}."""
    raise NotImplementedError

def suma_pares(numeros):
    """Suma SOLO los números pares de la lista (la lista vacía suma 0)."""
    total = 0
    for n in numeros:
        if n % 2 == 1:
            total += n
    return total

def a_snake_case(nombre):
    """Función que convierte CamelCase a snake_case: inserta '_' antes de cada mayúscula que no esté al principio y
    pasa todo a minúsculas. Ejemplos: 'MiVariable' -> 'mi_variable'; 'getHTTP' -> 'get_h_t_t_p'; 'hola'
    -> 'hola'."""
    raise NotImplementedError

def rango(numeros):
    """Función que devuelve la diferencia entre el mayor y el menor número. Si la lista está vacía lanza
    ValueError. Ejemplo: rango([3, 10, -2]) -> 12."""
    raise NotImplementedError

def email_valido(email):
    """Valida un email con estas reglas EXACTAS: contiene exactamente una '@'; antes de la '@' hay al menos un carácter;
    después de la '@' hay al menos un '.'; el último '.' no está pegado a la '@' ni es el último
    carácter. Ejemplos: 'ana@mail.com' -> True; 'ana@.com' -> False; 'anamail.com' -> False; 'ana@mail.'
    -> False."""
    raise NotImplementedError
