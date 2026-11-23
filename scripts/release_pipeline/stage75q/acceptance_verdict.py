"""Call the actual pinned build_release predicate; no acceptance evidence rewriting."""
from pathlib import Path
import hashlib,importlib.util,sys

def passed_verdict(text,agentos_root):
    root=Path(agentos_root).resolve()
    path=root/'scripts/build_release.py'
    name='_card63_build_verdict_'+hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0]=[str(root/'src'),str(root/'scripts')]
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        spec.loader.exec_module(module)
    return bool(sys.modules[name]._passed_verdict(str(text or '')))
