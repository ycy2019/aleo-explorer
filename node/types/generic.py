from collections.abc import Sequence

from .basic import *


class Generic(metaclass=ABCMeta):
    @abstractmethod
    def __init__(self, types):
        self.types = types

    def __class_getitem__(cls, item):
        if not isinstance(item, tuple):
            item = item,
        ## Unfortunately we have sized vec, so we can't have this check anymore
        # if not all(isinstance(x, type) or isinstance(x, Generic) for x in item):
        #     raise TypeError("expected type or generic types as generic types")
        return cls(item)


class TypeParameter:
    @abstractmethod
    def __init__(self, types):
        self.types = types

    def __class_getitem__(cls, item):
        if not isinstance(item, tuple):
            item = item,
        return cls(item)


class Tuple(Generic, Serialize, Deserialize, Sequence):

    def __init__(self, types):
        self.types = types

    def __call__(self, value: Sequence):
        if not isinstance(value, Sequence):
            raise TypeError("value must be Sequence")
        if len(value) != len(self.types):
            raise ValueError("value must have the same length as the tuple")
        for i, (t, v) in enumerate(zip(self.types, value)):
            if not isinstance(v, t):
                raise TypeError(f"value[{i}] must be {t}")
        self.value = tuple(value)

    def __len__(self):
        return len(self.value)

    def __iter__(self):
        return iter(self.value)

    def __getitem__(self, item):
        if not isinstance(item, int):
            raise TypeError("index must be int")
        return self.value[item]

    def dump(self) -> bytes:
        if not all(isinstance(x, Serialize) for x in self.value):
            raise TypeError("value must be serializable")
        return b"".join(x.dump() for x in self.value)

    # @type_check
    def load(self, data: bytearray):
        if not all(issubclass(x, Deserialize) for x in self.types):
            raise TypeError("value must be deserializable")
        self.value = tuple(t.load(data) for t in self.types)
        return self


class Vec(Generic, Serialize, Deserialize, Sequence):

    def __init__(self, types):
        if len(types) != 2:
            raise TypeError("expected 2 types for Vec")
        self.type = types[0]
        if isinstance(types[1], int):
            self.size = types[1]
        elif issubclass(types[1], Int):
            self.size_type = types[1]
        else:
            raise TypeError("expected int or Int as size type")
        super().__init__(types)

    def __call__(self, value):
        if not isinstance(value, list):
            raise TypeError("value must be list")
        if not all(isinstance(x, self.type) for x in value):
            raise TypeError("value must be of type {}".format(self.type))
        if hasattr(self, "size") and len(value) != self.size:
            raise ValueError("value must be of size {}".format(self.size))
        self._list = value
        if hasattr(self, "size_type"):
            self.size = len(value)
        return self

    def __len__(self):
        return len(self._list)

    def __getitem__(self, index):
        if not isinstance(index, int):
            raise TypeError("index must be int")
        return self._list[index]

    def dump(self) -> bytes:
        res = b""
        if hasattr(self, "size_type"):
            res += self.size_type.dump(self.size)
        for item in self._list:
            res += self.type.dump(item)
        return res

    # @type_check
    def load(self, data: bytearray):
        if not issubclass(self.type, Deserialize):
            raise TypeError(f"{self.type.__name__} must be Deserialize")
        if hasattr(self, "size_type"):
            if len(data) < self.size_type.size:
                raise ValueError("data is too short")
            self.size = self.size_type.load(data)
        self._list = []
        for i in range(self.size):
            # noinspection PyArgumentList
            self._list.append(self.type.load(data))
        return self

    def __iter__(self):
        return iter(self._list)


class VarInt(Generic, Serialize, Deserialize):

    def __init__(self, types):
        if len(types) != 1:
            raise TypeError("expected 1 type for VarInt")
        self.type = types[0]
        super().__init__(types)

    def __call__(self, value):
        if not isinstance(value, self.type):
            raise TypeError("value must be of type {}".format(self.type))
        self.value = value
        return self

    def dump(self) -> bytes:
        if 0 <= self.value <= 0xfc:
            return self.value.to_bytes(1, "little")
        elif 0xfd <= self.value <= 0xffff:
            return b"\xfd" + self.value.to_bytes(2, "little")
        elif 0x10000 <= self.value <= 0xffffffff:
            return b"\xfe" + self.value.to_bytes(4, "little")
        elif 0x100000000 <= self.value <= 0xffffffffffffffff:
            return b"\xff" + self.value.to_bytes(8, "little")
        else:
            raise ValueError("unreachable")

    # @type_check
    def load(self, data: bytearray):
        if len(data) == 0:
            raise ValueError("data is too short")
        if data[0] == 0xfd:
            if len(data) < 3:
                raise ValueError("data is too short")
            self.value = u16.load(data[1:])
            del data[:3]
        elif data[0] == 0xfe:
            if len(data) < 5:
                raise ValueError("data is too short")
            self.value = u32.load(data[1:])
            del data[:5]
        elif data[0] == 0xff:
            if len(data) < 9:
                raise ValueError("data is too short")
            self.value = u64.load(data[1:])
            del data[:9]
        else:
            self.value = u8.load(data)
            del data[:1]
        self.value = self.type(self.value)
        return self

class Option(Generic, Serialize, Deserialize):

    def __init__(self, types):
        if len(types) != 1:
            raise TypeError("expected 1 type for Option")
        self.type = types[0]
        super().__init__(types)

    def __call__(self, value):
        if value is None:
            self.value = None
        elif issubclass(type(self.type), Generic):
            if isinstance(self.type, Vec):
                if value.type != self.type.type and not (isinstance(value.type, Tuple) and isinstance(self.type.type, Tuple)):
                    raise TypeError(f"value should be {self.type}, but got {type(value)}")
            if isinstance(self.type, Tuple):
                if value.types != self.type.types:
                    raise TypeError(f"value should be {self.type}, but got {type(value)}")
        elif not isinstance(value, self.type):
            raise TypeError("value must be of type {} or None".format(self.type))
        self.value = value
        return self

    def dump(self) -> bytes:
        if self.value is None:
            return b"\x00"
        else:
            return b"\x01" + self.type.dump(self.value)

    def dumps(self) -> str | None:
        if self.value is None:
            return None
        else:
            return str(self.value)

    # @type_check
    def load(self, data: bytearray):
        is_some = bool_.load(data)
        if is_some:
            self.value = self.type.load(data)
        else:
            self.value = None
        return self

def generic_type_check(func):
    def wrapper(*args, **kwargs):
        hints = get_type_hints(func)
        for v, t in hints.items():
            arg = kwargs.get(v)
            if isinstance(t, Vec):
                if arg.type != t.type and not (isinstance(arg.type, Tuple) and isinstance(t.type, Tuple)):
                    raise TypeError(f"{v} should be {t}, but got {type(arg)}")
            if isinstance(t, Tuple):
                if arg.types != t.types:
                    raise TypeError(f"{v} should be {t}, but got {type(arg)}")
        return func(*args, **kwargs)

    return wrapper