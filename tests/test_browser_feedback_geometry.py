"""Capture keeps leaf content, private fields, clipping and top-layer geometry."""
import pytest

from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def mount_capture_scene(page):
    page.evaluate("""() => {
      const make=(tag,id,parent,style={})=>{
        const e=document.createElement(tag);e.id=id;parent.append(e);
        for(const [k,v] of Object.entries(style))e.style.setProperty(k,v);
        return e;
      };
      const scene=make('div','capture-scene',document.body,{position:'fixed',left:'20px',top:'120px',
        width:'300px',height:'260px','z-index':'1000000',background:'white'});
      make('div','capture-leaf',scene,{position:'absolute',left:'10px',top:'10px',width:'24px',height:'24px',background:'rgb(17, 211, 79)'});
      const publicField=make('input','capture-public',scene,{position:'absolute',top:'40px'});
      publicField.value='Public value';
      const secret=make('input','capture-secret',scene,{position:'absolute',top:'65px'});
      secret.type='password';secret.value='PRIVATE-CAPTURE-SECRET';
      const reason=make('textarea','capture-reason',scene,{position:'absolute',top:'90px'});
      reason.name='reason';reason.value='PRIVATE-CAPTURE-REASON';
      const scroller=make('div','capture-scroll',scene,{position:'absolute',top:'130px',width:'240px',height:'100px',overflow:'auto'});
      const content=make('div','capture-content',scroller,{height:'500px'});
      make('div','capture-sticky',content,{position:'sticky',top:'0px',height:'20px',background:'blue'});
      const clipped=make('div','capture-clipped',content,{'margin-top':'300px',width:'120px',height:'24px'});
      make('span','capture-clipped-text',clipped).textContent='Outside the scrollport';
      make('div','capture-outside-leaf',content,{width:'10px',height:'10px',background:'red'});
      make('div','capture-hidden',scene,{display:'none'});
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
      svg.id='capture-svg';svg.setAttribute('width','10');svg.setAttribute('height','10');scene.append(svg);
      const rect=document.createElementNS(svg.namespaceURI,'rect');rect.id='capture-svg-leaf';
      rect.setAttribute('width','10');rect.setAttribute('height','10');svg.append(rect);
      const topStyle={position:'fixed',left:'30px',top:'420px',width:'80px',height:'40px',margin:'0',padding:'0',border:'0'};
      make('dialog','capture-modal',document.body,topStyle);
      const popover=make('div','capture-popover',document.body,topStyle);popover.setAttribute('popover','manual');
      scroller.scrollTop=40;
    }""")


@pytest.mark.parametrize('scheme,viewport,dpr', [('light', {'width': 1440, 'height': 1000}, 1),
                                               ('dark', {'width': 390, 'height': 844}, 1),
                                               ('light', {'width': 390, 'height': 844}, 2)])
def test_capture_preserves_empty_leaves_private_values_sticky_and_top_layers(browser, live_server, h, scheme, viewport, dpr):
    context, page = sign_in(browser, live_server, 'staff', viewport=viewport, device_scale_factor=dpr)
    try:
        page.emulate_media(color_scheme=scheme)
        mount_capture_scene(page)
        result = page.evaluate("""async () => {
          const capture=await window.importBundle('feedback_capture.js');
          await capture.warmCapture();
          const s=capture.snapshot(),r=s.root,find=id=>r.querySelector('#capture-'+id);
          const canvas=await capture.render(s),pixel=[...canvas.getContext('2d').getImageData(42*devicePixelRatio,142*devicePixelRatio,1,1).data];
          return {leaf:!!find('leaf'),svgLeaf:!!find('svg-leaf'),outsideLeaf:!!find('outside-leaf'),
            hidden:!!find('hidden'),clippedChildren:find('clipped').childNodes.length,
            clippedSize:[find('clipped').style.width,find('clipped').style.height],
            publicValue:find('public').getAttribute('value'),
            secrets:[find('secret'),find('reason')].map(e=>({masked:e.hasAttribute('data-fb-mask'),value:e.getAttribute('value'),text:e.textContent})),
            leaked:r.outerHTML.includes('PRIVATE-CAPTURE-'),
            sticky:[find('sticky').style.position,find('sticky').style.top],translate:find('content').style.translate,
            pixel,size:[canvas.width,canvas.height],expectedSize:[innerWidth*devicePixelRatio,innerHeight*devicePixelRatio]};
        }""")
        assert result['leaf'] and result['svgLeaf'] and result['outsideLeaf'] and not result['hidden']
        assert result['clippedChildren'] == 0 and result['clippedSize'] == ['120px', '24px']
        assert result['publicValue'] == 'Public value' and not result['leaked']
        assert all(field == {'masked': True, 'value': '', 'text': ''} for field in result['secrets'])
        assert result['sticky'] == ['relative', '40px'] and result['translate'] == '0px -40px'
        assert result['size'] == result['expectedSize']
        assert all(abs(a-b) <= 1 for a, b in zip(result['pixel'], [17, 211, 79, 255])), result['pixel']
        for kind, opening, closing in [('modal', 'showModal', 'close'), ('popover', 'showPopover', 'hidePopover')]:
            top = page.evaluate("""async ([kind,opening,closing]) => {
              const source=document.querySelector('#capture-'+kind);source[opening]();
              try {
                const at=source.getBoundingClientRect(),s=(await window.importBundle('feedback_capture.js')).snapshot();
                const copy=s.root.querySelector('#capture-'+kind),px=n=>`${Math.round(n*100)/100}px`;
                return {empty:!source.hasChildNodes(),position:copy.style.position,
                  actual:[copy.style.left,copy.style.top,copy.style.width,copy.style.height],
                  expected:[at.left,at.top,at.width,at.height].map(px),backdrops:s.root.querySelectorAll('.fb-backdrop').length};
              } finally {source[closing]();}
            }""", [kind, opening, closing])
            assert top['empty'] and top['position'] == 'fixed' and top['actual'] == top['expected']
            assert top['backdrops'] == (1 if kind == 'modal' else 0)
    finally:
        context.close()
