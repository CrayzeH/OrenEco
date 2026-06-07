import sqlite3
import hashlib

conn = sqlite3.connect('oreneco.db')
cursor = conn.cursor()

password = 'admin123'
password_hash = hashlib.sha256(password.encode()).hexdigest()

cursor.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (password_hash,))
conn.commit()
conn.close()

print(f"✅ Пароль '{password}' обновлен")