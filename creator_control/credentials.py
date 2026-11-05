import ctypes as c
import json
import os
from ctypes import wintypes as w


class Credential(c.Structure):
    _fields_ = [
        ("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
        ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME), ("CredentialBlobSize", w.DWORD),
        ("CredentialBlob", c.POINTER(c.c_ubyte)), ("Persist", w.DWORD),
        ("AttributeCount", w.DWORD), ("Attributes", c.c_void_p),
        ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR),
    ]


class Vault:
    def __init__(self, workspace_id):
        if os.name != "nt":
            raise RuntimeError("Windows Credential Manager доступен в Windows.")
        self.prefix = f"CreatorControlCenter/{workspace_id}/"
        api = c.WinDLL("Advapi32", use_last_error=True)
        self.read = api.CredReadW
        self.read.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(c.POINTER(Credential)))
        self.read.restype = w.BOOL
        self.write = api.CredWriteW
        self.write.argtypes = (c.POINTER(Credential), w.DWORD)
        self.write.restype = w.BOOL
        self.delete = api.CredDeleteW
        self.delete.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD)
        self.delete.restype = w.BOOL
        self.free = api.CredFree
        self.free.argtypes = (c.c_void_p,)
        self.free.restype = None

    def get(self, key):
        pointer = c.POINTER(Credential)()
        if not self.read(self.prefix + key, 1, 0, c.byref(pointer)):
            if c.get_last_error() == 1168:
                return None
            raise c.WinError(c.get_last_error())
        try:
            credential = pointer.contents
            return json.loads(c.string_at(credential.CredentialBlob, credential.CredentialBlobSize))
        finally:
            self.free(pointer)

    def put(self, key, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(data) > 2560:
            raise ValueError("Запись превышает допустимый размер Windows Credential Manager.")
        buffer = (c.c_ubyte * len(data)).from_buffer_copy(data)
        item = Credential(Type=1, TargetName=self.prefix + key, CredentialBlobSize=len(data),
                          CredentialBlob=buffer, Persist=2, UserName="Creator Control Center")
        if not self.write(c.byref(item), 0):
            raise c.WinError(c.get_last_error())

    def remove(self, key):
        if not self.delete(self.prefix + key, 1, 0) and c.get_last_error() != 1168:
            raise c.WinError(c.get_last_error())
