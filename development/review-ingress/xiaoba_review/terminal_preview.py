"""Bounded, read-only text projection. Never read or write the PTY here."""
import codecs
from collections import deque
import re
import threading


class TerminalPreview:
    def __init__(self, lines=160, columns=1024):
        self._lock = threading.Lock()
        self._lines = deque(maxlen=lines)
        self._line = ''
        self._columns = columns
        self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._escape = None
        self._revision = 0
        self._trimmed = False
        self._line_sensitive = False

    @staticmethod
    def _safe(line):
        from xiaoba_agentos.memory import MemoryBridge
        if (MemoryBridge._sensitive_reason(line)
                or re.search(r'(?i)(?:bearer\s+|\bsk[-_]|(?:api[_ -]?key|password|secret|authorization|cookie|access[_ -]?token)\s*[=:])', line)):
            return '[敏感内容已隐藏]'
        return re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[账号已隐藏]', line)

    def feed(self, chunk):
        with self._lock:
            for char in self._decoder.decode(chunk):
                if self._escape == 'string':
                    if char == '\x07':self._escape = None
                    elif char == '\x1b':self._escape = 'string_end'
                    continue
                if self._escape == 'string_end':
                    self._escape = None if char == chr(92) else 'string'
                    continue
                if self._escape == 'csi':
                    if '@' <= char <= '~':self._escape = None
                    continue
                if self._escape == 'charset':
                    self._escape = None
                    continue
                if self._escape == 'escape':
                    self._escape = ('csi' if char == '[' else 'string' if char in ']P^_' else
                                    'charset' if char in '()' else None)
                    continue
                if char == '\x1b':self._escape = 'escape'
                elif char == '\n':
                    if len(self._lines) == self._lines.maxlen:self._trimmed = True
                    self._lines.append('[敏感内容已隐藏]' if self._line_sensitive else self._safe(self._line))
                    self._line = '';self._line_sensitive = False
                elif char == '\r':pass  # Preserve the output log, not cursor addressing.
                elif char == '\b':self._line = self._line[:-1]
                elif char == '\t':self._line += '    '
                elif char.isprintable():self._line += char
                if len(self._line) > self._columns:
                    self._line_sensitive |= self._safe(self._line) == '[敏感内容已隐藏]'
                    self._line = self._line[-self._columns:];self._trimmed = True
            self._revision += 1

    def set_screen(self, text):
        # Use the already reconstructed screen, not an append-only TUI log.
        # Sanitize the full screen before limiting lines, so a wrapped secret
        # cannot survive merely because its field name was clipped away.
        safe = self._safe(text)
        lines = safe.split('\n')
        with self._lock:
            limit = self._lines.maxlen
            self._trimmed = len(lines)>limit or any(len(line)>self._columns for line in lines)
            lines = [line[:self._columns] for line in lines[-limit:]]
            self._lines.clear();self._lines.extend(lines[:-1])
            self._line = lines[-1] if lines else ''
            self._line_sensitive = False
            self._revision += 1

    def snapshot(self):
        with self._lock:
            current = '[敏感内容已隐藏]' if self._line_sensitive else self._safe(self._line)
            return {'text':'\n'.join([*self._lines, current]),
                    'revision':self._revision, 'trimmed':self._trimmed,
                    'format':'terminal_text', 'read_only':True,
                    'retention':'bounded_process_memory_only'}
