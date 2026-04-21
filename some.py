from cryptography.fernet import Fernet
import secrets

print("FERNET_SECRET_KEY=" + Fernet.generate_key().decode())
print("ADMIN_SECRET_TOKEN=" + secrets.token_urlsafe(32))