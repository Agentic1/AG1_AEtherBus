import os
import time
import threading
import traceback
import queue
import curses
import json
import textwrap
from dotenv import load_dotenv
import redis

# Load .env in script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(script_dir, ".env")
load_dotenv(dotenv_path)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_USERNAME = os.getenv("REDIS_USERNAME")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
POLL_INTERVAL = 0.5
MAX_TAIL_LINES = 10

# Initialize Redis client
if REDIS_USERNAME and REDIS_PASSWORD:
    db = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        username=REDIS_USERNAME,
        password=REDIS_PASSWORD,
        decode_responses=False
    )
else:
    db = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)

# Test connection
try:
    db.ping()
    connection_status = f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}"
except Exception as e:
    connection_status = f"Redis connection error: {e}"

# Shared state
filter_text = ""
tail_queue = queue.Queue()
error_queue = queue.Queue()
subscriptions = []
message_tail = []
last_error_lines = []
selected_idx = 0
height = width = mid = None
last_ids = {}

# Poller thread: discover and read Redis streams

def poll_streams():
    global filter_text, last_ids
    while True:
        try:
            keys = [k.decode() for k in db.keys("*")]
            streams = [k for k in keys if filter_text in k]
            for key in streams:
                # skip non-stream keys
                try:
                    if db.type(key) != b'stream':
                        continue
                except:
                    error_queue.put(traceback.format_exc())
                    continue
                # read new entries
                last_id = last_ids.get(key, '0-0')
                entries = db.xread({key: last_id}, count=1, block=int(POLL_INTERVAL * 1000))
                if entries:
                    for _, msgs in entries:
                        for msg_id, data in msgs:
                            tail_queue.put((key, msg_id.decode(), data))
                            last_ids[key] = msg_id.decode()
            # remove streams no longer matched
            for k in list(last_ids):
                if k not in streams:
                    del last_ids[k]
        except:
            error_queue.put(traceback.format_exc())
        time.sleep(POLL_INTERVAL)

# Popup display with wrapped lines

def show_popup(stdscr, lines):
    h, w = stdscr.getmaxyx()
    max_line = max((len(line) for line in lines), default=0)
    ph = min(len(lines) + 2, h - 4)
    pw = min(max_line + 2, w - 4)
    win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    win.border()
    y = 1
    for line in lines:
        for segment in textwrap.wrap(line, pw - 2):
            if y < ph - 1:
                win.addnstr(y, 1, segment, pw - 2)
                y += 1
            else:
                break
    win.getch()
    win.clear()
    stdscr.touchwin()
    stdscr.refresh()

# Main TUI

def bus_tui(stdscr):
    global filter_text, subscriptions, message_tail, last_error_lines, selected_idx, height, width, mid
    curses.curs_set(0)
    stdscr.nodelay(True)
    height, width = stdscr.getmaxyx()
    mid = int(width * 0.2)

    streams_win = curses.newwin(height - 5, mid, 0, 0)
    messages_win = curses.newwin(height - 5, width - mid - 1, 0, mid + 1)
    status_win = curses.newwin(5, width, height - 5, 0)

    while True:
        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == ord('f'):
            # prompt for filter
            curses.echo()
            curses.curs_set(1)
            stdscr.nodelay(False)
            status_win.erase()
            status_win.addstr(0, 2, "Filter: ")
            status_win.refresh()
            try:
                nf = status_win.getstr(0, 10, mid - 12).decode()
                if nf != filter_text:
                    filter_text = nf
                    selected_idx = 0
                    message_tail.clear()
                    last_ids.clear()
            except:
                pass
            curses.noecho()
            curses.curs_set(0)
            stdscr.nodelay(True)
        elif key == curses.KEY_UP:
            selected_idx = max(0, selected_idx - 1)
        elif key == curses.KEY_DOWN:
            selected_idx = min(len(subscriptions) - 1, selected_idx + 1)
        elif key in (10, 13):
            # enter focuses on stream
            if subscriptions:
                filter_text = subscriptions[selected_idx]
                message_tail.clear()
                selected_idx = 0
        elif key == ord('o'):
            # open packet (extract and pretty-print payload)
            if 0 <= selected_idx < len(message_tail):
                _, _, data = message_tail[selected_idx]
                try:
                    raw = data.get(b'data', data)
                    if isinstance(raw, bytes):
                        raw = raw.decode('utf-8', errors='replace')
                except Exception:
                    raw = str(data)
                try:
                    obj = json.loads(raw)
                    lines = json.dumps(obj, indent=2).splitlines()
                except Exception:
                    lines = raw.splitlines()
                show_popup(stdscr, lines)
        elif key == ord('r'):
            # related by correlation_id
            if 0 <= selected_idx < len(message_tail):
                _, _, data = message_tail[selected_idx]
                try:
                    cid = json.loads(data).get("correlation_id")
                except:
                    cid = None
                related = []
                if cid:
                    for st, mid_id, dt in message_tail:
                        try:
                            pkt = json.loads(dt)
                            if pkt.get("correlation_id") == cid:
                                related.append(dt)
                        except:
                            continue
                if not related:
                    related = ["No related packets"]
                show_popup(stdscr, related)
            if 0 <= selected_idx < len(message_tail):
                _, _, data = message_tail[selected_idx]
                try:
                    cid = json.loads(data).get("correlation_id")
                except:
                    cid = None
                related = []
                if cid:
                    for st, mid_id, dt in message_tail:
                        try:
                            pkt = json.loads(dt)
                            if pkt.get("correlation_id") == cid:
                                related.append(dt)
                        except:
                            continue
                if not related:
                    related = ["No related packets"]
                show_popup(stdscr, related)

            # related by correlation_id
            if 0 <= selected_idx < len(message_tail):
                _, _, data = message_tail[selected_idx]
                try:
                    cid = json.loads(data).get("correlation_id")
                except:
                    cid = None
                related = []
                if cid:
                    for st, mid_id, dt in message_tail:
                        try:
                            pkt = json.loads(dt)
                            if pkt.get("correlation_id") == cid:
                                related.append(dt)
                        except:
                            continue
                if not related:
                    related = ["No related packets"]
                show_popup(stdscr, related)

        # update subscriptions list
        try:
            allk = [k.decode() for k in db.keys("*")]
        except:
            error_queue.put(traceback.format_exc())
            allk = []
        subscriptions = [k for k in allk if filter_text in k]

        # draw streams pane
        streams_win.erase()
        streams_win.border()
        streams_win.addstr(0, 2, " Streams ")
        for idx, s in enumerate(subscriptions[:height - 7]):
            if idx == selected_idx:
                streams_win.attron(curses.A_REVERSE)
            streams_win.addnstr(idx + 1, 2, s, mid - 4)
            if idx == selected_idx:
                streams_win.attroff(curses.A_REVERSE)
        streams_win.refresh()

        # fetch and filter messages
        try:
            while True:
                st_key, msg_id, dt = tail_queue.get_nowait()
                if filter_text and filter_text not in st_key:
                    continue
                message_tail.insert(0, (st_key, msg_id, dt))
                if len(message_tail) > MAX_TAIL_LINES:
                    message_tail.pop()
        except queue.Empty:
            pass
        except:
            error_queue.put(traceback.format_exc())

        # draw messages pane
        messages_win.erase()
        messages_win.border()
        messages_win.addstr(0, 2, " Recent Messages (o=open, r=related) ")
        for idx, (st_key, msg_id, dt) in enumerate(message_tail[:height - 7]):
            text = f"[{st_key}] {msg_id}: {dt}"
            messages_win.addnstr(idx + 1, 1, text, width - mid - 3)
        messages_win.refresh()

        # show errors
        try:
            err = error_queue.get_nowait()
            last_error_lines = err.splitlines()[:2]
        except queue.Empty:
            pass

        # draw status pane
        status_win.erase()
        status_win.addstr(0, 2, connection_status[:width - 4])
        status_text = (
            f"q:quit f:filter o:open r:relate |"
            f" Filter='{filter_text}' | Streams={len(subscriptions)} | Msgs={len(message_tail)}"
        )
        status_win.addnstr(1, 2, status_text, width - 4)
        for i, line in enumerate(last_error_lines):
            status_win.addnstr(2 + i, 2, f"Error: {line}", width - 4)
        status_win.refresh()

        time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    threading.Thread(target=poll_streams, daemon=True).start()
    curses.wrapper(bus_tui)
