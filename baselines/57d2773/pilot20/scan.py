from pathlib import Path
from collections import Counter
import re,json,csv,posixpath
from m2.src.parsing.gathering import strip_comments
root=Path(__file__).parent; hits=[]; other=[]; graphics=[]; commands=Counter()
for a in sorted((root/'preserve').glob('papers/*/versions/*/source_assets_audit.json')):
 d=json.loads(a.read_text());files={i['path'] for i in d['inventory'] if i['disposition']=='filtered'}; ver=(d['arxiv_id'],d['version'])
 for f in (a.parent/'original_assets').rglob('*'):
  if f.suffix.lower()!='.tex':continue
  fn=f.relative_to(a.parent/'original_assets').as_posix();tx=strip_comments(f.read_bytes().decode('latin1'))[0]
  for m in re.finditer(r'\\(input|include|subfile|bibliography|addbibresource|pgfplotstable[a-zA-Z]*|addplot[a-zA-Z]*|includegraphics)\b\*?(?:\s*\[[^\]]*\])*\s*(?:\{([^{}]+)\}|([^\s{}%]+))?',tx):
   cmd=m[1];targets=m[2] or m[3] or '';commands[cmd]+=1
   for raw in targets.split(','):
    raw=raw.strip()
    if not raw:continue
    paths={posixpath.normpath(posixpath.join(b,raw)) for b in ('',posixpath.dirname(fn))}
    for n in sorted(files):
     if cmd=='includegraphics':
      if any(n==p or Path(n).stem==Path(p).name or Path(n).name==Path(p).name for p in paths):graphics.append((*ver,fn,tx.count('\n',0,m.start())+1,cmd,raw,n))
     elif n in paths or Path(n).name==Path(raw).name or any(n==p+ext for p in paths for ext in ('.tex','.bib','.bbl')):
      hits.append((*ver,fn,tx.count('\n',0,m.start())+1,cmd,raw,n))
  for n in sorted(files):
   if Path(n).suffix.lower() in ('.png','.pdf','.jpg'):continue
   for i,line in enumerate(tx.splitlines(),1):
    if re.search(r'(?<![\w.-])'+re.escape(Path(n).name)+r'(?![\w.-])',line):other.append((*ver,fn,i,n,line.strip()[:200]))
print('scan',commands,'hits',hits,'other',other[:45],len(other),'graphics',len(graphics),len({(r[0],r[1],r[-1]) for r in graphics}))
for name,rows in [('graphics',graphics),('filtered',hits),('mentions',other)]:
 with (root/(name+'.tsv')).open('w') as f:csv.writer(f,delimiter='\t').writerows(rows)
