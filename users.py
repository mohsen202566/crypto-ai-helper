import json
import os

from config import OWNER_ID

USERS_FILE = "users.json"


def load_users():
    if not os.path.exists(USERS_FILE):
        return [OWNER_ID]

    try:
        with open(USERS_FILE, "r") as f:
            users = json.load(f)

        if OWNER_ID not in users:
            users.append(OWNER_ID)

        return users
    except Exception:
        return [OWNER_ID]


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)


def is_owner(user_id):
    return user_id == OWNER_ID


def is_user_allowed(user_id):
    return user_id in load_users()


def add_user(user_id):
    users = load_users()

    if user_id not in users:
        users.append(user_id)
        save_users(users)

    return True


def remove_user(user_id):
    if user_id == OWNER_ID:
        return False

    users = load_users()

    if user_id in users:
        users.remove(user_id)
        save_users(users)
        return True

    return False


def list_users():
    return load_users()
