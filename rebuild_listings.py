"""Hide games/movies/giveaways from main feed + book genre listings; build category/other*.html."""
import json,re,os,glob
r=json.load(open('reviews.json'))
O={'Video Games','Movie','Movies & Entertainment','Asher Boys','Board Games','Giveaway','Family Fun'}
OTHER={e['slug'] for e in r if (set(e['categories'])&O or 'giveaway' in e['title'].lower()) and 'booky' not in (e['slug']+e['title']).lower()}
OTHER_CATS={'video-games','movie','movies-entertainment','asher-boys','board-games','giveaway','family-fun','other'}
SPLIT='<div class="cards-grid">'
def parse(f):
    h=open(f).read()
    i=h.index(SPLIT)+len(SPLIT); j=h.index('</div><div class="pagination">',i)
    k=h.index('</div>',j+len('</div><div class="pagination">'))+6
    cards=['<div class="review-card">'+c for c in h[i:j].split('<div class="review-card">')[1:]]
    # last card ends with </div> that closed grid? grid close is at j
    return h[:i],cards,h[j:j+6],h[k:]
def slug(card): return re.search(r'href="(?:\.\./)?posts/([^"]+)\.html"',card).group(1)
def series(base,d=''):
    p=os.path.join(d,base+'.html'); files=[p]; n=2
    pre='page-' if base=='index' else base+'-page-'
    while os.path.exists(os.path.join(d,f'{pre}{n}.html')): files.append(os.path.join(d,f'{pre}{n}.html')); n+=1
    return files,pre
def pagelink(base,pre,n): return f'{base}.html' if n==1 else f'{pre}{n}.html'
def write(base,d,files,pre,cards,templates):
    pages=[cards[i:i+12] for i in range(0,len(cards),12)] or [[]]
    N=len(pages)
    for n,pc in enumerate(pages,1):
        f=os.path.join(d,pagelink(base,pre,n))
        if os.path.exists(f): head,_,_,tail=parse(f)
        else:
            head,_,_,tail=templates[-1]
            head=head.replace(os.path.basename(files[-1]),os.path.basename(f))
        pg='<div class="pagination">'
        if n>1: pg+=f'<a href="{pagelink(base,pre,n-1)}">&laquo; Prev</a>'
        pg+=f'<span class="current">Page {n} of {N}</span>'
        if n<N: pg+=f'<a href="{pagelink(base,pre,n+1)}">Next &raquo;</a>'
        pg+='</div>'
        open(f,'w').write(head+''.join(pc)+'</div>'+pg+tail)
    # leftover pages: redirect, keep URL live
    for f in files[N:]:
        tgt=pagelink(base,pre,N)
        open(f,'w').write(f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="robots" content="noindex"><meta http-equiv="refresh" content="0; url={tgt}"><link rel="canonical" href="{tgt}"><title>BookGateway</title></head><body><a href="{tgt}">Continue</a></body></html>')
    return N
stats={}
# main feed
files,pre=series('index')
parsed=[parse(f) for f in files]; allcards=[c for p in parsed for c in p[1]]
feed=[c for c in allcards if slug(c) not in OTHER]
stats['index']=(len(allcards),len(feed),write('index','',files,pre,feed,parsed))
# other listing from main-feed order (newest first)
other=[c.replace('href="posts/','href="../posts/').replace('href="category/','href="../category/') for c in allcards if slug(c) in OTHER]
ab,_=series('asher-boys','category'); tpl=parse(ab[0])
head=tpl[0]
head=re.sub(r'Asher Boys','Other: Games, Movies &amp; Giveaways',head)
head=head.replace('category/asher-boys.html','category/other.html')
open('category/other.html','w').write(head+'</div><div class="pagination"></div>'+tpl[3])
of,opre=series('other','category')
stats['other']=(len(other),write('other','category',of,opre,other,[parse(of[0])]))
# book genre listings
for f in sorted(glob.glob('category/*.html')):
    b=os.path.basename(f)[:-5]
    if '-page-' in b or b in OTHER_CATS: continue
    files,pre=series(b,'category'); parsed=[parse(x) for x in files]
    cs=[c for p in parsed for c in p[1]]; keep=[c for c in cs if slug(c) not in OTHER]
    if len(keep)!=len(cs): stats[b]=(len(cs),len(keep),write(b,'category',files,pre,keep,parsed))
print(len(OTHER)); [print(k,v) for k,v in stats.items()]
