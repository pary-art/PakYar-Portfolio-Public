"""Protect the separate clinical database with Windows access controls."""
from __future__ import annotations

import os
from pathlib import Path


def protect_private_storage(path: Path) -> None:
    """Restrict files to the local service identity and Windows administrators."""
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            path.chmod(0o700)
            return
        except OSError as exc:
            raise RuntimeError('محافظت از پوشهٔ پرونده خصوصی ممکن نیست.') from exc
    try:
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        allowed = [
            win32security.GetTokenInformation(token, win32security.TokenUser)[0],
            win32security.LookupAccountName(None, 'SYSTEM')[0],
            win32security.LookupAccountName(None, 'BUILTIN\\Administrators')[0],
        ]
        acl = win32security.ACL()
        inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        unique_allowed = []
        for sid in allowed:
            if not any(sid == existing for existing in unique_allowed):
                unique_allowed.append(sid)
        for sid in unique_allowed:
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inherit,
                                      win32con.GENERIC_ALL, sid)
        security_flags = (win32security.DACL_SECURITY_INFORMATION
                          | win32security.PROTECTED_DACL_SECURITY_INFORMATION)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                           security_flags, None, None, acl, None)
        for item in path.iterdir():
            if item.is_file():
                win32security.SetNamedSecurityInfo(str(item), win32security.SE_FILE_OBJECT,
                                                   security_flags, None, None, acl, None)
    except Exception as exc:
        # The server must not start with an unprotected store of private clinical notes.
        raise RuntimeError('دسترسی فایل پرونده خصوصی را نمی‌توان محدود کرد؛ شروع برنامه متوقف شد.') from exc


def encrypt_clinical_snapshot(data: bytes) -> bytes:
    if os.name != 'nt':
        return b'PBK1' + data
    import win32crypt
    encrypted = win32crypt.CryptProtectData(
        data, 'PakYar occupational-health backup', None, None, None, 0,
    )
    if isinstance(encrypted, tuple):
        encrypted = encrypted[-1]
    return b'PBK1' + encrypted


def decrypt_clinical_snapshot(payload: bytes) -> bytes:
    if not payload.startswith(b'PBK1'):
        raise ValueError('پشتیبان خصوصی سالم یا سازگار نیست.')
    if os.name != 'nt':
        return payload[4:]
    import win32crypt
    _description, data = win32crypt.CryptUnprotectData(payload[4:], None, None, None, 0)
    return data
