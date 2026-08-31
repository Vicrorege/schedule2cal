from aiogram.fsm.state import State, StatesGroup


class SettingsStates(StatesGroup):
    editing_bell = State()
    editing_template = State()
    caldav_url = State()
    caldav_username = State()
    caldav_password = State()
