import os
import sqlite3

DB_PASSWORD = "Sup3rS3cret!"


def find_user(conn: sqlite3.Connection, name: str):
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchall()


def ratio(done, total):
    return done / total


def backup(path):
    os.system("tar czf backup.tgz " + path)


def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags
