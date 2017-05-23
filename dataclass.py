# TODO:

#  what exception to raise when non-default follows default? currently
#  ValueError

#  what to do if a user specifies a function we're going to overwrite,
#  like __init__? error? overwrite it?

#  use typing.get_type_hints() instead of accessing __annotations__
#  directly? recommended by PEP 526, but that's importing a lot just
#  to get at __annotations__

# if needed for efficiency, compute self_tuple and other_tuple just once, and pass them around

import collections

__all__ = ['dataclass', 'field']

_MISSING = "MISSING"
_MARKER = '__dataclass_fields__'
_SELF_NAME = '_self'
_OTHER_NAME = '_other'


# XXX: can't use slots, because we fill in name later
# maybe create another (derived?) type that adds the name, so we can use slots?
# not sure how many of these we're going to have
class field:
    ## __slots__ = ('name',
    ##              'default',
    ##              'repr',
    ##              'hash',
    ##              'init',
    ##              'cmp',
    ##              )
    def __init__(self, *, default=_MISSING, repr=True, hash=True, init=True, cmp=True):
        self.name = None  # added later
        self.default = default
        self.repr = repr
        self.hash = hash
        self.init = init
        self.cmp = cmp

    # XXX: currently for testing. either complete this, or delete it
    def __repr__(self):
        return f'field({self.name})'


def _to_field_definition(type):
    return type


def _tuple_str(obj_name, fields):
    # Return a string representing each field of obj_name as a tuple
    #  member. So, if fields is ['x', 'y'] and obj_name is "self",
    #  return "(self.x,self.y)".

    #Special case for the 0-tuple
    if len(fields) == 0:
        return '()'
    # Note the trailing comma, needed for 1-tuple
    return f'({",".join([f"{obj_name}.{f.name}" for f in fields])},)'


def _create_fn(name, args, body, locals=None):
    # Note that we mutate locals. Caller beware!
    if locals is None:
        locals = {}
    args = ','.join(args)
    body = '\n'.join(f' {b}' for b in body)
    txt = f'def {name}({args}):\n{body}'
    #print(txt)
    exec(txt, None, locals)
    return locals[name]


def _field_init(info):
    if info.default == _MISSING:
        # There's no default, just use the value from our parameter list.
        return f'{_SELF_NAME}.{info.name} = {info.name}'

    if isinstance(info.default, (list, dict, set)):
        # We're a type we know how to copy. If no default is given, copy the default.
        return f'{_SELF_NAME}.{info.name} = {_SELF_NAME}.__class__.{info.name}.copy() if {info.name} is {_SELF_NAME}.__class__.{info.name} else {info.name}'

    # XXX Is our default a factory function?
    return f'{_SELF_NAME}.{info.name} = {info.name}'


def _init(fields):
    # Make sure we don't have fields without defaults following fields
    #  with defaults.  If I switch to building the source to the
    #  __init__ function and compiling it, this isn't needed, since it
    #  will catch the problem.
    seen_default = False
    for f in fields:
        if f.default is not _MISSING:
            seen_default = True
        else:
            if seen_default:
                raise ValueError(f'non-default argument {f.name} follows default argument')

    args = [_SELF_NAME] + [(f.name if f.default is _MISSING else f"{f.name}=_def_{f.name}") for f in fields]
    body_lines = [_field_init(f) for f in fields]
    if len(body_lines) == 0:
        body_lines = ['pass']

    # Locals contains defaults, supply them.
    locals = {f'_def_{f.name}': f.default for f in fields if f.default is not _MISSING}
    return _create_fn('__init__',
                      args,
                      body_lines,
                      locals)


def _repr(fields):
    return _create_fn('__repr__',
                      [f'{_SELF_NAME}'],
                      [f'return {_SELF_NAME}.__class__.__name__ + f"(' + ','.join([f"{f.name}={{{_SELF_NAME}.{f.name}!r}}" for f in fields]) + ')"'],
                      )


def _create_cmp_fn(name, op, fields):
    self_tuple = _tuple_str(_SELF_NAME, fields)
    other_tuple = _tuple_str(_OTHER_NAME, fields)
    return _create_fn(name,
                      [_SELF_NAME, _OTHER_NAME],
                      [f'if {_OTHER_NAME}.__class__ is '
                          f'{_SELF_NAME}.__class__:',
                       f'    return {self_tuple}{op}{other_tuple}',
                        'return NotImplemented'],
                      )


def _eq(fields):
    return _create_cmp_fn('__eq__', '==', fields)


def _ne():
    # __ne__ is slightly different, use a different pattern.
    return _create_fn('__ne__',
                      [_SELF_NAME, _OTHER_NAME],
                      [f'result = {_SELF_NAME}.__eq__({_OTHER_NAME})',
                        'return NotImplemented if result is NotImplemented '
                            'else not result',
                       ],
                      )


def _lt(fields):
    return _create_cmp_fn('__lt__', '<',  fields)


def _le(fields):
    return _create_cmp_fn('__le__', '<=', fields)


def _gt(fields):
    return _create_cmp_fn('__gt__', '>',  fields)


def _ge(fields):
    return _create_cmp_fn('__ge__', '>=', fields)


def _hash(fields):
    self_tuple = _tuple_str(_SELF_NAME, fields)
    return _create_fn('__hash__',
                      [_SELF_NAME],
                      [f'return hash({self_tuple})'])


def _find_fields(cls):
    # Return a list tuples of tuples of (name, field()), in order, for
    #  this class (and no subclasses).  Fields are found from
    #  __annotations__.  Default values are from class attributes, if
    #  a field has a default.

    # Note that the type (as retrieved from __annotations__) is only
    #  used to identify fields.  The actual value of the type
    #  annotation is not saved anywhere.  It can be retrieved from
    #  __annotations__ if needed.

    annotations = getattr(cls, '__annotations__', {})

    results = []
    for name, type in annotations.items():
        # If the default value isn't derived from field, then it's
        # only a normal default value.  Convert it to a field().
        default = getattr(cls, name, _MISSING)
        if not isinstance(default, field):
            default = field(default=default)
        results.append((name, default))
    return results


def _field_filter(fields, predicate):
    return [f for f in fields if predicate(f)]


class Factory:
    pass


def dataclass(_cls=None, *, repr=True, cmp=True, hash=None, init=True,
               slots=False, frozen=False):
    def wrap(cls):
        fields = collections.OrderedDict()
        our_fields = []

        # In reversed order so that most derived class overrides earlier
        #  definitions.
        for m in reversed(cls.__mro__):
            # Only process classes marked with our decorator, or our own
            #  class.
            if hasattr(m, _MARKER):
                # This is a base class, collect the fields we've
                #  already processed.
                for name, f in _find_fields(m):
                    fields[name] = f
            elif m is cls:
                # This is our class, process each field we find in it.
                for name, info in _find_fields(m):
                    fields[name] = info
                    our_fields.append(info)

                    # XXX: instead of mutating info, maybe copy
                    # this to a different object with the same
                    # fields, but adding name?
                    info.name = name

                    # Field validations for fields directly on our
                    # class.  This is delayed until now, instead
                    # of in the field() constructor, since only
                    # here do we know the field name, which allows
                    # better error reporting.

                    # If init=False, we must have a default value.
                    #  Otherwise, how would it get initialized?
                    if not info.init and info.default == _MISSING:
                        raise ValueError(f'field {name} has init=False, but '
                                         'has no default value')

                    # If the class attribute (which is the default
                    #  value for this field) exists and is of type
                    #  'field', replace it with the real default.
                    #  This is so that normal class introspection sees
                    #  a real default value.
                    if isinstance(getattr(cls, name, None), field):
                        setattr(cls, name, info.default)
            else:
                # Not a base class we care about
                pass

        # We've de-duped and have the fields in order, no longer need
        # a dict of them.
        fields = list(fields.values())

        # Remember the total set of fields on our class (included bases).
        setattr(cls, _MARKER, fields)

        if init:
            cls.__init__ = _init(_field_filter(fields, lambda f: f.init))
        if repr:
            cls.__repr__ = _repr(_field_filter(fields, lambda f: f.repr))
        cls.__hash__ = _hash(_field_filter(fields, lambda f: f.hash))

        if cmp:
            # Create comparison functions.
            cmp_fields = _field_filter(fields, lambda f: f.cmp)
            cls.__eq__ = _eq(cmp_fields)
            cls.__ne__ = _ne()
            cls.__lt__ = _lt(cmp_fields)
            cls.__le__ = _le(cmp_fields)
            cls.__gt__ = _gt(cmp_fields)
            cls.__ge__ = _ge(cmp_fields)

        return cls

    # See if we're being called as @dataclass or @dataclass().
    if _cls is None:
        # We're called as @dataclass()
        return wrap

    # We're called as @dataclass, with a class
    return wrap(_cls)
