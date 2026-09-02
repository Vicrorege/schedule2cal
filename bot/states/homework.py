from aiogram.fsm.state import State, StatesGroup


class HomeworkStates(StatesGroup):
    waiting_for_confirm = State()
