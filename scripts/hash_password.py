"""Create a bcrypt hash for DASHBOARD_PASSWORD_HASH. Usage: python -m scripts.hash_password"""
import getpass

import bcrypt

pw = getpass.getpass("Dashboard password: ")
if len(pw) < 12:
    raise SystemExit("use at least 12 characters")
if pw != getpass.getpass("Repeat: "):
    raise SystemExit("passwords differ")
print(bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode())
