import re, sys
p = "review_loop/contained.py"
s = open(p).read()
anchor = '"--ro-bind-try", "/etc/alternatives", "/etc/alternatives",'
if anchor not in s:
    print("anchor not found")
    sys.exit(1)
s = s.replace(anchor, anchor + ' "--ro-bind", "/", "/",', 1)
open(p, "w").write(s)
print("mutated: + --ro-bind / /")