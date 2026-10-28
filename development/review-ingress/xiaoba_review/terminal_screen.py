"""Bounded terminal screen for native TUI cursor/erase output, not a shell."""
import codecs
import unicodedata


class TerminalScreen:
    def __init__(self, rows=80, columns=240):
        self.rows,self.columns=rows,columns
        self.grid=[[' ']*columns for _ in range(rows)]
        self.x=self.y=0;self.saved=(0,0);self.top=0;self.bottom=rows-1
        self.state='text';self.sequence=''
        self.decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')

    def _down(self):
        if self.y==self.bottom:
            self.grid.pop(self.top);self.grid.insert(self.bottom,[' ']*self.columns)
        else:self.y=min(self.rows-1,self.y+1)

    def _reverse(self):
        if self.y==self.top:
            self.grid.insert(self.top,[' ']*self.columns);self.grid.pop(self.bottom+1)
        else:self.y=max(0,self.y-1)

    def _csi(self, final):
        self.x=min(self.x,self.columns-1)
        private=self.sequence.startswith('?')
        try:args=[int(v) if v else 0 for v in self.sequence.lstrip('?').split(';')]
        except ValueError:return
        n=(args[0] or 1);mode=args[0]
        if private:return
        if final=='A':self.y=max(self.top,self.y-n)
        elif final in 'Be':self.y=min(self.bottom,self.y+n)
        elif final in 'Ca':self.x=min(self.columns-1,self.x+n)
        elif final=='D':self.x=max(0,self.x-n)
        elif final=='E':self.y=min(self.bottom,self.y+n);self.x=0
        elif final=='F':self.y=max(self.top,self.y-n);self.x=0
        elif final=='G':self.x=min(self.columns-1,n-1)
        elif final=='d':self.y=min(self.rows-1,n-1)
        elif final in 'Hf':
            self.y=min(self.rows-1,n-1)
            self.x=min(self.columns-1,((args[1] or 1) if len(args)>1 else 1)-1)
        elif final=='K':
            start=self.x if mode==0 else 0;end=self.x+1 if mode==1 else self.columns
            self.grid[self.y][start:end]=[' ']*(end-start)
        elif final=='J':
            if mode in (2,3):self.grid=[[' ']*self.columns for _ in range(self.rows)]
            elif mode==0:
                self.grid[self.y][self.x:]=[' ']*(self.columns-self.x)
                for y in range(self.y+1,self.rows):self.grid[y]=[' ']*self.columns
            elif mode==1:
                for y in range(self.y):self.grid[y]=[' ']*self.columns
                self.grid[self.y][:self.x+1]=[' ']*(self.x+1)
        elif final in '@PX':
            line=self.grid[self.y];n=min(n,self.columns-self.x)
            if final=='@':line[self.x:self.x]=[' ']*n;del line[self.columns:]
            elif final=='P':del line[self.x:self.x+n];line.extend([' ']*n)
            else:line[self.x:self.x+n]=[' ']*n
        elif final in 'LMST':
            start=self.y if final in 'LM' else self.top
            for _ in range(min(n,self.bottom-start+1)):
                if final in 'LT':self.grid.insert(start,[' ']*self.columns);self.grid.pop(self.bottom+1)
                else:self.grid.pop(start);self.grid.insert(self.bottom,[' ']*self.columns)
        elif final=='r':
            top=n-1;bottom=(args[1] or self.rows)-1 if len(args)>1 else self.rows-1
            if 0<=top<bottom<self.rows:self.top,self.bottom=top,bottom;self.x=self.y=0
        elif final=='s':self.saved=(self.x,self.y)
        elif final=='u':self.x,self.y=self.saved

    def feed(self, raw):
        for char in self.decoder.decode(raw):
            if self.state=='string':
                if char==chr(7):self.state='text'
                elif char==chr(27):self.state='string_end'
                continue
            if self.state=='string_end':self.state='text' if char==chr(92) else 'string';continue
            if self.state=='charset':self.state='text';continue
            if self.state=='csi':
                if '@'<=char<='~':self._csi(char);self.state='text'
                elif len(self.sequence)<64:self.sequence+=char
                else:self.state='text'
                continue
            if self.state=='escape':
                self.state='text'
                if char=='[':self.state='csi';self.sequence=''
                elif char in ']P^_':self.state='string'
                elif char in '()':self.state='charset'
                elif char=='7':self.saved=(self.x,self.y)
                elif char=='8':self.x,self.y=self.saved
                elif char=='D':self._down()
                elif char=='E':self.x=0;self._down()
                elif char=='M':self._reverse()
                continue
            if char==chr(27):self.state='escape'
            elif char=='\r':self.x=0
            elif char=='\n':self._down()
            elif char=='\b':self.x=max(0,self.x-1)
            elif char=='\t':self.x=min(self.columns-1,(self.x//8+1)*8)
            elif char.isprintable():
                if unicodedata.combining(char):
                    if self.x:self.grid[self.y][min(self.x-1,self.columns-1)]+=char
                    continue
                width=2 if unicodedata.east_asian_width(char) in ('W','F') else 1
                if self.x+width>self.columns:self.x=0;self._down()
                self.grid[self.y][self.x]=char
                if width==2:self.grid[self.y][self.x+1]=''
                self.x+=width

    def text(self):
        return '\n'.join(''.join(row).rstrip() for row in self.grid).rstrip()
