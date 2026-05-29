from config import OWNER_ID

ALLOWED_USERS_DYNAMIC = set([OWNER_ID])


def is_owner(user_id):
    return user_id == OWNER_ID


def is_user_allowed(user_id):
    return user_id in ALLOWED_USERS_DYNAMIC


def add_user(user_id):
    ALLOWED_USERS_DYNAMIC.add(user_id)
    return True


def remove_user(user_id):
    if user_id == OWNER_ID:
        return False

    if user_id in ALLOWED_USERS_DYNAMIC:
        ALLOWED_USERS_DYNAMIC.remove(user_id)
        return True

    return False


def list_users():
    return list(ALLOWED_USERS_DYNAMIC)
