"""Offline TUI repaint tests; never counted as a real model response."""
import unittest
from xiaoba_review.terminal_screen import TerminalScreen


class ScreenTests(unittest.TestCase):
    def test_cursor_up_erase_and_partial_writes_reconstruct_one_result(self):
        screen=TerminalScreen();esc=chr(27)
        stream=('XB_BEGIN_TEST\r\n{"output":"par\r\n'+esc+'[1A\r'+esc+'[2K'+
                '{"output":"complete"}\r\nXB_END_TEST\r\n> ')
        for byte in stream.encode():screen.feed(bytes([byte]))
        self.assertEqual(screen.text(),'XB_BEGIN_TEST\n{"output":"complete"}\nXB_END_TEST\n>')
        self.assertNotIn('par',screen.text())

    def test_cursor_horizontal_overwrites_and_clear_to_end(self):
        screen=TerminalScreen();esc=chr(27)
        screen.feed(('a fake stale result'+esc+'[1G'+esc+'[0J'+'accepted').encode())
        self.assertEqual(screen.text(),'accepted')

    def test_full_frame_repaint_does_not_accumulate_stale_markers(self):
        screen=TerminalScreen();esc=chr(27)
        screen.feed(('old marker'+esc+'[2J'+esc+'[H'+'new frame').encode())
        self.assertEqual(screen.text(),'new frame')

    def test_unicode_cells_wrap_and_osc_are_bounded(self):
        screen=TerminalScreen(rows=2,columns=8);esc=chr(27)
        stream='中文ab'+esc+']52;c;TEST_NOT_SCREEN'+chr(7)+'cdEF'
        for byte in stream.encode():screen.feed(bytes([byte]))
        self.assertEqual(screen.text(),'中文abcd\nEF')
        self.assertNotIn('TEST_NOT_SCREEN',screen.text())
        self.assertEqual(len(screen.grid),2)
