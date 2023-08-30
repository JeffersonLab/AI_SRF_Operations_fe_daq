import concurrent
import tkinter as tk
import tkinter.ttk as ttk
from datetime import datetime
from tkinter.messagebox import askyesno
import logging
from typing import List, Union
from enum import Enum



logger = logging.getLogger(__name__)


def convert_ced_to_epics_name(name):
    zone_map = {
        '02': '2', '03': '3', '04': '4', '05': '5', '06': '6',
        '07': '7', '08': '8', '09': '9', '10': 'A',
        '11': 'B', '12': 'C', '13': 'D', '14': 'E',
        '15': 'F', '16': 'G', '17': 'H', '18': 'I',
        '19': 'J', '20': 'K', '21': 'L', '22': 'M',
        '23': 'N', '24': 'O', '25': 'P', '26': 'Q'
    }

    linac = name[0:1]
    zone = zone_map[name[2:4]]
    cav = name[5:6]

    return f"R{linac}{zone}{cav}"


def user_alert_scan_paused(msg: str, level: int = logging.WARNING) -> bool:
    """Launch a yes/no GUI that displays error message.  Returns true if scan should continue.
    Args:
        msg: An informative message to display about the pause
        level: The logging level to use for these messages.  Defaults to logging.WARNING.
    """

    logger.log(level=level, msg=msg)
    root = tk.Tk()
    root.overrideredirect(1)
    root.withdraw()
    do_continue = tk.messagebox.askyesno(title="FE Gradient Scan Paused", message=f"{msg}\n\nContinue?", width=160)
    root.destroy()
    if do_continue:
        logger.log(level=level, msg=f"User requested yes (continue/retry)")
    else:
        logger.log(level=level, msg=f"User requested no (exit/abort)")

    return do_continue


def user_alert_update_gsets_fail(failed: List["Cavity"]):
    msg = f"Cavities {','.join([cav.name for cav in failed])} failed to update."
    logger.error(msg)

    # Setup basic GUI elements
    response = ""
    var = tk.StringVar("R")
    root = tk.Tk()
    label1 = tk.Label(text=msg, width=160)
    label1.pack()

    # Add radio buttons
    options = {
        "R": "Retry Cavity Updates",
        "A": "Total Abort (PSETs restored)",
        "S": "Skip cavity, Rollback Gradients",
        "K": "Accept current gradients for this sample"
    }
    for (value, text) in options.items():
        ttk.Radiobutton(root, text=text, variable=var, value=value).pack(side=tk.LEFT)

    # Set up the form submission
    def handle_click(event: tk.Event):
        nonlocal response
        response = var.get()
        root.destroy()

    submit = tk.Button(text="Continue")
    submit.bind("<Button-1>", handle_click)
    submit.pack(side=tk.RIGHT)

    root.mainloop()

    logger.info(f"Received response {response} - {options[response]}.)")

    status = Status.UNKNOWN
    if response == 'R':
        status = Status.RETRY
        # # Recurse back into this function and try again.  Return the eventual status of follow on
        # # attempts.
        # status = update_cavity_gsets_parallel(cavs, new_gsets, settle, force)
    elif response == 'S':
        status = Status.FAIL
        # Call this function again to put the gradients back to their original settings.  Return FAIL.
        # update_cavity_gsets_parallel(cavs, orig_gsets, settle, force)
        # status = Status.FAIL
    elif response == 'A':
        # The user requested we stop the procedure all together and make no more changes than to restore
        # PSETs
        status = Status.ABORT
    elif response == 'K':
        # Something went wrong, but the user thinks something updated successfully.  We'll call it success.
        status = Status.SUCCESS
    else:
        raise ValueError(f"Unrecognized selection {response}")

    return status


class Status(Enum):
    """An Enum class for tracking the state of a gradient update."""
    # Everything worked - collect data
    SUCCESS = 1
    # Something went wrong, but we want to try it again
    RETRY = 2
    # Something went wrong - skip data, continue procedure
    FAIL = 3
    # Something went really wrong - skip data, abort procedure
    ABORT = 4
    # We haven't finished the job to find out how it went
    UNKNOWN = 5




def write_data_index_header(file, **kwargs):
    """Writes a generic header line for the mya data lookup index.

    Args:
        file: Is a writeable file object
        kwargs: Dictionary will be written to the first line if any are specified
    """
    if len(kwargs) > 0:
        file.write(f"# {kwargs}\n")
    file.write(f"#settle_start,settle_end,avg_start,avg_end,settle_dur,avg_dur,cavity_name,cavity_epics_name\n")


def write_data_index_row(file, settle_start: datetime, settle_end: datetime, avg_start: datetime, avg_end: datetime,
                         settle_time: float, avg_time: float, cavity_name: Union[str, List[str]],
                         cavity_epics_name: Union[str, List[str]]):
    """Write a standard entry to the data index file.

    Args:
        file: A writeable file object
        settle_start: The start time for the (cryo) settle period
        settle_end: The end time for the (cryo) settle period
        avg_start: The start time for the pure data collection period (may be used for averaging, hence the name)
        avg_end: The end time for the pure data collection period
        settle_time: The duration in seconds of the settle time
        avg_time:  The duration in seconds of the pure data collection period
        cavity_name:  The CED names of cavity that was altered.
        cavity_epics_name:  The EPICS names of the cavity that was altered
    """
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    settle_start_str = settle_start.strftime(fmt)
    settle_end_str = settle_end.strftime(fmt)
    avg_start_str = avg_start.strftime(fmt)
    avg_end_str = avg_end.strftime(fmt)

    # Convert a list-like into a delimited string
    cn = cavity_name
    if type(cavity_name).__name__ != 'str':
        cn = ':'.join(cavity_name)

    cen = cavity_epics_name
    if type(cavity_epics_name).__name__ != 'str':
        cen = ':'.join(cavity_epics_name)

    file.write(f"{settle_start_str},{settle_end_str},{avg_start_str},{avg_end_str},{settle_time},"
               f"{avg_time},{cn},{cen}\n")


