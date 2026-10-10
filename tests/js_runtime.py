"""Run dependency-free JS checks with Node, or macOS JavaScriptCore."""
import ctypes
import json
import shutil
import subprocess
import sys
import unittest


def evaluate(source):
    if shutil.which('node'):
        result = subprocess.run(['node', '-e', f'console.log(eval({json.dumps(source)}))'],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    if sys.platform != 'darwin':
        raise unittest.SkipTest('Client checks require Node or macOS JavaScriptCore')
    js = ctypes.CDLL('/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore')
    ptr = ctypes.c_void_p
    signatures = {
        'JSGlobalContextCreate': ([ptr], ptr),
        'JSGlobalContextRelease': ([ptr], None),
        'JSStringCreateWithUTF8CString': ([ctypes.c_char_p], ptr),
        'JSStringRelease': ([ptr], None),
        'JSEvaluateScript': ([ptr, ptr, ptr, ptr, ctypes.c_int, ctypes.POINTER(ptr)], ptr),
        'JSValueToStringCopy': ([ptr, ptr, ctypes.POINTER(ptr)], ptr),
        'JSStringGetMaximumUTF8CStringSize': ([ptr], ctypes.c_size_t),
        'JSStringGetUTF8CString': ([ptr, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_size_t),
    }
    for name, (args, result) in signatures.items():
        getattr(js, name).argtypes = args
        getattr(js, name).restype = result
    ctx = js.JSGlobalContextCreate(None)
    script = js.JSStringCreateWithUTF8CString(source.encode())
    try:
        error = ptr()
        result = js.JSEvaluateScript(ctx, script, None, None, 1, ctypes.byref(error))
        string = js.JSValueToStringCopy(ctx, error.value or result, None)
        try:
            buf = ctypes.create_string_buffer(js.JSStringGetMaximumUTF8CStringSize(string))
            js.JSStringGetUTF8CString(string, buf, len(buf))
            text = buf.value.decode()
        finally:
            js.JSStringRelease(string)
        if error.value:
            raise AssertionError(text)
        return json.loads(text)
    finally:
        js.JSStringRelease(script)
        js.JSGlobalContextRelease(ctx)
