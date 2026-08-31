from aiogram.fsm.state import State, StatesGroup


class ScheduleStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_class = State()
    waiting_for_subgroup = State()
    waiting_for_date = State()
    naming_lessons = State()
    waiting_for_review = State()
    editing_lesson = State()
    processing = State()
