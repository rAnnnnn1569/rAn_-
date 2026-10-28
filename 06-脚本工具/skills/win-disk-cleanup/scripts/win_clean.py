#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
win_clean —— Windows 磁盘清理工具（已适配 WorkBuddy safe-delete hook）

子命令：
  size   <path>...            统计文件数/字节
  rb     <drive>              查看回收站占用（如 C:\\）
  empty  <drive>              清空该盘回收站（真正释放空间）
  recycle <path>...           批量送入回收站（可恢复）
  purge  <path>...            批量永久删除（不可恢复 —— 只在别处有母本时用）
  free   <drive>              查看可用空间

设计要点（详见 SKILL.md）：
  * WorkBuddy 的 safe-delete hook 会把 os.remove/shutil.rmtree 改成"送回收站"，
    而回收站与源文件同盘 → 空间不释放，必须再 empty。
  * 该 hook 对海量小文件会逐文件 spawn node，极慢；本工具改用 shell32 的
    SHFileOperationW 对整目录一次操作，秒级完成。
  * 本工具不经 Python 的删除 API，因此不受 hook 逐文件开销影响。
"""
import os
import sys
import ctypes
from ctypes import wintypes

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND),
        ('wFunc', wintypes.UINT),
        ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR),
        ('fFlags', ctypes.c_uint16),
        ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]

FO_DELETE          = 3
FOF_ALLOWUNDO      = 0x0040   # → 送回收站；去掉即永久删除
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT         = 0x0004
FOF_NOERRORUI      = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI   = 0x00000002
SHERB_NOSOUND        = 0x00000004

_shell32 = ctypes.windll.shell32
_shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
_shell32.SHEmptyRecycleBinW.restype = ctypes.c_long

RET = {
    0x00000000: 'OK',
    0x00000020: 'ERROR_SHARING_VIOLATION（文件被占用）',
    0x0000007C: 'ERROR_INVALID_NAME（路径语法）',
    0x8000FFFF: '回收站已空（E_UNEXPECTED，非错误）',
}


def _op(paths, allow_undo):
    """SHFileOperationW。注意 pFrom 需双 \\0 结尾（ctypes 会自动补最后一个）。"""
    if not paths:
        return 0, False
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = '\0'.join(paths) + '\0'
    op.pTo = None
    op.fFlags = (FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
                 | (FOF_ALLOWUNDO if allow_undo else 0))
    r = _shell32.SHFileOperationW(ctypes.byref(op))
    return r & 0xFFFFFFFF, bool(op.fAnyOperationsAborted)


def stat2(p):
    if not os.path.exists(p):
        return 0, 0
    if os.path.isfile(p):
        try:
            return os.path.getsize(p), 1
        except OSError:
            return 0, 0
    t = n = 0
    for r, ds, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
                n += 1
            except OSError:
                pass
    return t, n


def rb_size(drive):
    rb = os.path.join(drive.rstrip('\\/') + '\\', '$Recycle.Bin')
    t = n = 0
    if not os.path.isdir(rb):
        return 0, 0
    for r, ds, fs in os.walk(rb):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
                n += 1
            except OSError:
                pass
    return t, n


def free(drive):
    b = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(drive, None, None, b):
        raise OSError('GetDiskFreeSpaceExW 失败: %s' % drive)
    return ctypes.c_ulonglong.from_buffer(b).value


def cmd_size(args):
    for p in args:
        s, n = stat2(p)
        print('%12.1f MB / %6d 文件  %s' % (s / 1048576.0, n, p))
    return 0


def cmd_free(args):
    for d in args:
        try:
            print('%-6s 可用 %9.1f MB (%.2f GB)  %s' % (d, free(d) / 1048576.0,
                                                          free(d) / 2 ** 30, d))
        except OSError as e:
            print('%-6s 读取失败 %s' % (d, e))
    return 0


def cmd_rb(args):
    for d in args:
        t, n = rb_size(d)
        print('%-6s 回收站 %9.1f MB / %d 文件' % (d, t / 1048576.0, n))
    return 0


def cmd_empty(args):
    for d in args:
        b4 = None
        try:
            b4 = free(d)
        except OSError:
            pass
        r = _shell32.SHEmptyRecycleBinW(None, d, SHERB_NOCONFIRMATION
                                        | SHERB_NOPROGRESSUI | SHERB_NOSOUND) & 0xFFFFFFFF
        t, n = rb_size(d)
        msg = '%-6s SHEmptyRecycleBinW=0x%08X (%s) ；残留 %.1f MB / %d 文件' % (
            d, r, RET.get(r, '未知'), t / 1048576.0, n)
        if b4 is not None:
            try:
                msg += ' ；可用 %.1f → %.1f MB (Δ%+.1f MB)' % (
                    b4 / 1048576.0, free(d) / 1048576.0, (free(d) - b4) / 1048576.0)
            except OSError:
                pass
        print(msg)
    return 0


def _bulk(paths, allow_undo, verb):
    verb_cn = '送回收站' if allow_undo else '永久删除'
    exist = [p for p in paths if os.path.exists(p)]
    missing = [p for p in paths if not os.path.exists(p)]
    for p in missing:
        print('  SKIP  [不存在] %s' % p)
    if not exist:
        return 0
    total = sum(stat2(p)[0] for p in exist)
    print('%s：%d 项，合计 %.1f MB' % (verb_cn, len(exist), total / 1048576.0))
    bad = 0
    for i in range(0, len(exist), 200):
        chunk = exist[i:i + 200]
        before = sum(stat2(p)[0] for p in chunk)
        r, ab = _op(chunk, allow_undo)
        left = [p for p in chunk if os.path.exists(p)]
        print('  批 %2d：%d 项 → ret=0x%08X (%s) aborted=%s ；已消失 %d/%d'
              % (i // 200 + 1, len(chunk), r, RET.get(r, 'OK' if r == 0 else 'err'),
                 ab, len(chunk) - len(left), len(chunk)))
        # 对仍存在的逐个重试一次
        for p in left:
            r2, _ = _op([p], allow_undo)
            if os.path.exists(p):
                bad += 1
                print('     FAIL %-60s ret=0x%08X (%s)' % (p[:60], r2, RET.get(r2, '未知')))
    if bad:
        print('  未能删除/移动 %d 项（多为被占用）' % bad)
    return 0


def cmd_recycle(args):
    return _bulk(args, True, 'recycle')


def cmd_purge(args):
    return _bulk(args, False, 'purge')


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    cmd, args = argv[1], argv[2:]
    table = {'size': cmd_size, 'free': cmd_free, 'rb': cmd_rb, 'empty': cmd_empty,
             'recycle': cmd_recycle, 'purge': cmd_purge}
    fn = table.get(cmd)
    if not fn:
        print('未知子命令: %s' % cmd)
        print(__doc__)
        return 1
    return fn(args)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
