#!/usr/bin/env python3
"""Idempotent: replace favicon/icon/apple-touch-icon/manifest links in every page <head>."""
import re,glob
TAGS=open('/workspace/favicons/bookgateway/link-tags.html').read().strip()
RX=re.compile(r'[ \t]*<link\b[^>]*\brel=["\'](?:shortcut icon|icon|apple-touch-icon|manifest|mask-icon)["\'][^>]*>\s*\n?',re.I)
changed=0;nohead=[]
for f in sorted(glob.glob('*.html')+glob.glob('*/*.html')):
    s=open(f,encoding='utf-8').read()
    m=re.search(r'<head[^>]*>',s,re.I)
    if not m: nohead.append(f); continue
    h_end=s.lower().find('</head>')
    head=s[m.end():h_end] if h_end!=-1 else None
    if head is None: nohead.append(f); continue
    newhead='\n'+TAGS+'\n'+RX.sub('',head).lstrip('\n')
    n=s[:m.end()]+newhead+s[h_end:]
    if n!=s: open(f,'w',encoding='utf-8').write(n); changed+=1
print('changed',changed,'nohead',nohead)
