import sys, zipfile, re, html
def main():
    src, dst = sys.argv[1], sys.argv[2]
    z = zipfile.ZipFile(src)
    xml = z.read('word/document.xml').decode('utf-8', 'replace')
    xml = xml.replace('</w:p>', '\n')
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    xml = re.sub(r'<w:br[^>]*/>', '\n', xml)
    txt = re.sub(r'<[^>]+>', '', xml)
    txt = html.unescape(txt)
    lines = [re.sub(r'[ \t\xa0]+',' ',l).strip() for l in txt.split('\n')]
    res=[]; blank=0
    for s in lines:
        if s=='':
            blank+=1
            if blank<=1: res.append('')
        else:
            blank=0; res.append(s)
    open(dst,'w',encoding='utf-8').write('\n'.join(res))
    print('OK', dst, len(res), 'lines')
main()
