import json
import os
from typing import Dict, List
from ..config import settings

# Default if file is missing
DEFAULT_SCHEDULE = {
    "Microwave":         ["CiCi", "Atlas", "Anabelle", "Roach", "Izzy", "Thorin", "Lynn"],
    "Snickers":          ["Megan", "Felix", "Brooke", "Acacia", "Rinne", "Emmaleigh", "Elusive"],
    "Business":          ["Atlas", "Alexa", "Morgan", "Bunny", "Abigail", "Zoe", "Elusive"],
    "The Greens":        ["Jaeden", "Isabella", "Julia", "Micaela", "Brooke", "Peter", "Elusive"],
    "HOP":               ["Jaeden", "Victoria", "Anabelle", "Brian", "Sophia", "Victoria", "Sophia"],
    "Lot 50":            ["Miranda", "Brian", "Bryan", "Brian", "Bryan", "Zahara", "Miranda"],
    "Mary Kay and Zen":  ["Kitadan", "Emma", "Kitadan", "Kitadan", "Jack", "Jack", "Jack"],
    "West Hall":         ["Loren", "Charlotte", "Autumn", "Michael", "Loren", "Roach", "Emmaleigh"],
    "Maintenance":       ["Emma", "Lucas", "Izzy", "Izzy", "Morgan", "Izzy", "Lucas"],
}

def _ensure_file_exists():
    path = settings.schedule_file
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_SCHEDULE, f, indent=2)

def load_schedule() -> Dict[str, List[str]]:
    _ensure_file_exists()
    try:
        with open(settings.schedule_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading schedule: {e}")
        return DEFAULT_SCHEDULE

def save_schedule(data: Dict[str, List[str]]) -> bool:
    _ensure_file_exists()
    try:
        with open(settings.schedule_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving schedule: {e}")
        return False