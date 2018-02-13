#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

import string
import sys
from functools import lru_cache
from gettext import gettext as _

from kitty.config import cached_values_for
from kitty.fast_data_types import wcswidth
from kitty.key_encoding import (
    DOWN, ESCAPE, F1, F2, F3, LEFT, RELEASE, RIGHT, SHIFT, TAB, UP,
    backspace_key, enter_key
)

from ..tui.handler import Handler
from ..tui.loop import Loop
from ..tui.operations import (
    clear_screen, color_code, colored, cursor, set_line_wrapping,
    set_window_title, sgr, styled
)

HEX, NAME, EMOTICONS = 'HEX', 'NAME', 'EMOTICONS'


@lru_cache(maxsize=256)
def points_for_word(w):
    from .unicode_names import codepoints_for_word
    return codepoints_for_word(w)


@lru_cache(maxsize=4096)
def name(cp):
    from .unicode_names import name_for_codepoint
    if isinstance(cp, str):
        cp = ord(cp[0])
    return (name_for_codepoint(cp) or '').capitalize()


@lru_cache(maxsize=256)
def codepoints_matching_search(text):
    parts = text.lower().split()
    ans = []
    if parts and parts[0]:
        codepoints = points_for_word(parts[0])
        for word in parts[1:]:
            pts = points_for_word(word)
            if pts:
                intersection = codepoints & pts
                if intersection:
                    codepoints = intersection
                    continue
            codepoints = {c for c in codepoints if word in name(c).lower()}
        if codepoints:
            ans = list(sorted(codepoints))
    return ans


FAINT = 242
DEFAULT_SET = tuple(map(
    ord,
    '‘’“”‹›«»‚„' '😀😛😇😈😉😍😎😮👍👎' '—–§¶†‡©®™' '→⇒•·°±−×÷¼½½¾'
    '…µ¢£€¿¡¨´¸ˆ˜' 'ÀÁÂÃÄÅÆÇÈÉÊË' 'ÌÍÎÏÐÑÒÓÔÕÖØ' 'ŒŠÙÚÛÜÝŸÞßàá' 'âãäåæçèéêëìí'
    'îïðñòóôõöøœš' 'ùúûüýÿþªºαΩ∞'
))
EMOTICONS_SET = tuple(range(0x1f600, 0x1f64f + 1))


def encode_hint(num, digits=string.digits + string.ascii_lowercase):
    res = ''
    d = len(digits)
    while not res or num > 0:
        num, i = divmod(num, d)
        res = digits[i] + res
    return res


def decode_hint(x):
    return int(x, 36)


class Table:

    def __init__(self):
        self.layout_dirty = True
        self.last_rows = self.last_cols = -1
        self.codepoints = []
        self.current_idx = 0
        self.text = ''
        self.num_cols = self.num_rows = 0
        self.mode = HEX

    @property
    def current_codepoint(self):
        if self.codepoints:
            return self.codepoints[self.current_idx]

    def set_codepoints(self, codepoints, mode=HEX):
        self.codepoints = codepoints
        self.mode = mode
        self.layout_dirty = True
        self.current_idx = 0

    def codepoint_at_hint(self, hint):
        return self.codepoints[decode_hint(hint)]

    def layout(self, rows, cols):
        if not self.layout_dirty and self.last_cols == cols and self.last_rows == rows:
            return self.text
        self.last_cols, self.last_rows = cols, rows
        self.layout_dirty = False

        if self.mode is NAME:
            def as_parts(i, codepoint):
                return encode_hint(i).ljust(idx_size), chr(codepoint), name(codepoint)

            def cell(i, idx, c, desc):
                is_current = i == self.current_idx
                if is_current:
                    yield sgr(color_code('gray', base=40))
                yield colored(idx, 'green') + ' '
                yield colored(c, 'black' if is_current else 'gray', True) + ' '
                w = wcswidth(c)
                if w < 2:
                    yield ' ' * (2 - w)
                if len(desc) > space_for_desc:
                    desc = desc[:space_for_desc - 1] + '…'
                yield colored(desc, FAINT)
                extra = space_for_desc - len(desc)
                if extra > 0:
                    yield ' ' * extra
                if is_current:
                    yield sgr('49')
        else:
            def as_parts(i, codepoint):
                return encode_hint(i).ljust(idx_size), chr(codepoint), ''

            def cell(i, idx, c, desc):
                yield colored(idx, 'green') + ' '
                yield colored(c, 'gray', True) + ' '
                w = wcswidth(c)
                if w < 2:
                    yield ' ' * (2 - w)

        num = len(self.codepoints)
        if num < 1:
            self.text = ''
            self.num_cols = self.num_rows = 0
            return self.text
        idx_size = len(encode_hint(num - 1))

        parts = [as_parts(i, c) for i, c in enumerate(self.codepoints)]
        if self.mode is NAME:
            sizes = [idx_size + 2 + len(p[2]) + 2 for p in parts]
        else:
            sizes = [idx_size + 3 for p in parts]
        longest = max(sizes) if sizes else 0
        col_width = longest + 2
        col_width = min(col_width, 40)
        space_for_desc = col_width - 2 - idx_size - 4
        num_cols = self.num_cols = cols // col_width
        buf = []
        a = buf.append
        rows_left = self.num_rows = rows

        for i, (idx, c, desc) in enumerate(parts):
            if i > 0 and i % num_cols == 0:
                rows_left -= 1
                if rows_left == 0:
                    break
                buf.append('\r\n')
            buf.extend(cell(i, idx, c, desc))
            a('  ')
        self.text = ''.join(buf)
        return self.text

    def move_current(self, rows=0, cols=0):
        if cols:
            self.current_idx = (self.current_idx + len(self.codepoints) + cols) % len(self.codepoints)
            self.layout_dirty = True
        if rows:
            amt = rows * self.num_cols
            self.current_idx += amt
            self.current_idx = max(0, min(self.current_idx, len(self.codepoints) - 1))
            self.layout_dirty = True


class UnicodeInput(Handler):

    def __init__(self, cached_values):
        self.cached_values = cached_values
        self.recent = list(self.cached_values.get('recent', DEFAULT_SET))
        self.current_input = ''
        self.current_char = None
        self.prompt_template = '{}> '
        self.last_updated_code_point_at = None
        self.choice_line = ''
        self.mode = globals().get(cached_values.get('mode', 'HEX'), 'HEX')
        self.table = Table()
        self.update_prompt()

    def update_codepoints(self):
        codepoints = None
        if self.mode is HEX:
            q = self.mode, None
            codepoints = self.recent
        elif self.mode is EMOTICONS:
            q = self.mode, None
            codepoints = list(EMOTICONS_SET)
        elif self.mode is NAME:
            q = self.mode, self.current_input
            if q != self.last_updated_code_point_at:
                codepoints = codepoints_matching_search(self.current_input)
        if q != self.last_updated_code_point_at:
            self.last_updated_code_point_at = q
            self.table.set_codepoints(codepoints, self.mode)

    def update_current_char(self):
        self.update_codepoints()
        self.current_char = None
        if self.mode is HEX:
            try:
                if self.current_input.startswith('r') and len(self.current_input) > 1:
                    self.current_char = chr(self.table.codepoint_at_hint(self.current_input[1:]))
                else:
                    code = int(self.current_input, 16)
                    self.current_char = chr(code)
            except Exception:
                pass
        elif self.mode is NAME:
            cc = self.table.current_codepoint
            if cc:
                self.current_char = chr(cc)
        else:
            try:
                if self.current_input:
                    self.current_char = chr(self.table.codepoint_at_hint(self.current_input))
            except Exception:
                pass
        if self.current_char is not None:
            code = ord(self.current_char)
            if code <= 32 or code == 127 or 128 <= code <= 159 or 0xd800 <= code <= 0xdbff or 0xDC00 <= code <= 0xDFFF:
                self.current_char = None

    def update_prompt(self):
        self.update_current_char()
        if self.current_char is None:
            c, color = '??', 'red'
            self.choice_line = ''
        else:
            c, color = self.current_char, 'green'
            self.choice_line = _('Chosen:') + ' {} U+{} {}'.format(
                colored(c, 'green'), hex(ord(c))[2:], styled(name(c) or '', italic=True, fg=FAINT))
        self.prompt = self.prompt_template.format(colored(c, color))

    def initialize(self, *args):
        Handler.initialize(self, *args)
        self.write(set_line_wrapping(False))
        self.write(set_window_title(_('Unicode input')))
        self.draw_screen()

    def draw_title_bar(self):
        entries = []
        for name, key, mode in [
                (_('Code'), 'F1', HEX),
                (_('Name'), 'F2', NAME),
                (_('Emoji'), 'F3', EMOTICONS),
        ]:
            entry = ' {} ({}) '.format(name, key)
            if mode is self.mode:
                entry = styled(entry, reverse=False, bold=True)
            entries.append(entry)
        text = _('Search by:{}').format(' '.join(entries))
        extra = self.screen_size.cols - wcswidth(text)
        if extra > 0:
            text += ' ' * extra
        self.print(styled(text, reverse=True))

    def draw_screen(self):
        self.write(clear_screen())
        self.draw_title_bar()
        y = 1

        def writeln(text=''):
            nonlocal y
            self.print(text)
            y += 1

        if self.mode is NAME:
            writeln(_('Enter the hex code for the character'))
        elif self.mode is HEX:
            writeln(_('Enter words from the name of the character'))
        else:
            writeln(_('Enter the index for the character you want from the list below'))
        self.write(self.prompt)
        self.write(self.current_input)
        with cursor(self.write):
            writeln()
            writeln(self.choice_line)
            if self.mode is HEX:
                writeln(styled(_('Type {} followed by the index for the recent entries below').format('r'), fg=FAINT))
            elif self.mode is NAME:
                writeln(styled(_('Use Tab or the arrow keys to choose a character from below'), fg=FAINT))
            self.table_at = y
            self.write(self.table.layout(self.screen_size.rows - self.table_at, self.screen_size.cols))

    def refresh(self):
        self.update_prompt()
        self.draw_screen()

    def on_text(self, text, in_bracketed_paste):
        self.current_input += text
        self.refresh()

    def on_key(self, key_event):
        if key_event is backspace_key:
            self.current_input = self.current_input[:-1]
            self.refresh()
        elif key_event is enter_key:
            self.quit_loop(0)
        elif key_event.type is RELEASE:
            if key_event.key is ESCAPE:
                self.quit_loop(1)
            elif key_event.key is F1:
                self.switch_mode(HEX)
            elif key_event.key is F2:
                self.switch_mode(NAME)
            elif key_event.key is F3:
                self.switch_mode(EMOTICONS)
        elif self.mode is NAME:
            if key_event.key is TAB:
                if key_event.mods == SHIFT:
                    self.table.move_current(cols=-1), self.refresh()
                elif not key_event.mods:
                    self.table.move_current(cols=1), self.refresh()
            elif key_event.key is LEFT and not key_event.mods:
                self.table.move_current(cols=-1), self.refresh()
            elif key_event.key is RIGHT and not key_event.mods:
                self.table.move_current(cols=1), self.refresh()
            elif key_event.key is UP and not key_event.mods:
                self.table.move_current(rows=-1), self.refresh()
            elif key_event.key is DOWN and not key_event.mods:
                self.table.move_current(rows=1), self.refresh()

    def switch_mode(self, mode):
        if mode is not self.mode:
            self.mode = mode
            self.cached_values['mode'] = mode
            self.current_input = ''
            self.current_char = None
            self.choice_line = ''
            self.refresh()

    def on_interrupt(self):
        self.quit_loop(1)

    def on_eot(self):
        self.quit_loop(1)

    def on_resize(self, new_size):
        Handler.on_resize(self, new_size)
        self.refresh()


def main(args=sys.argv):
    loop = Loop()
    with cached_values_for('unicode-input') as cached_values:
        handler = UnicodeInput(cached_values)
        loop.loop(handler)
        if handler.current_char and loop.return_code == 0:
            print('OK:', hex(ord(handler.current_char))[2:])
            try:
                handler.recent.remove(ord(handler.current_char))
            except Exception:
                pass
            recent = [ord(handler.current_char)] + handler.recent
            cached_values['recent'] = recent[:len(DEFAULT_SET)]
    raise SystemExit(loop.return_code)