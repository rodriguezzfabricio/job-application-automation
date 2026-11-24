/** Run inside the Gmail connector call. Return only non-secret matching metadata. */
export function verificationMetadata(messages, expected) {
  function sha256(text) {
    const bytes=Array.from(unescape(encodeURIComponent(text)),c=>c.charCodeAt(0));
    const bitLength=bytes.length*8;
    bytes.push(128);
    while(bytes.length%64!==56) bytes.push(0);
    for(let i=7;i>=0;i--) bytes.push(Math.floor(bitLength/2**(8*i))%256);
    const constants=[], state=[];
    for(let p=2;constants.length<64;p++){
      let prime=true;for(let d=2;d*d<=p;d++)if(p%d===0){prime=false;break;}
      if(!prime)continue;
      if(state.length<8)state.push((Math.sqrt(p)*2**32)|0);
      constants.push((Math.cbrt(p)*2**32)|0);
    }
    const rotate=(x,n)=>(x>>>n)|(x<<(32-n));
    for(let i=0;i<bytes.length;i+=64){
      const w=new Int32Array(64);
      for(let j=0;j<16;j++){const k=i+j*4;w[j]=(bytes[k]<<24)|(bytes[k+1]<<16)|(bytes[k+2]<<8)|bytes[k+3];}
      for(let j=16;j<64;j++){
        const x=w[j-15],y=w[j-2];
        w[j]=w[j-16]+(rotate(x,7)^rotate(x,18)^(x>>>3))+w[j-7]+(rotate(y,17)^rotate(y,19)^(y>>>10));
      }
      let [a,b,c,d,e,f,g,h]=state;
      for(let j=0;j<64;j++){
        const t=h+(rotate(e,6)^rotate(e,11)^rotate(e,25))+((e&f)^(~e&g))+constants[j]+w[j];
        const u=(rotate(a,2)^rotate(a,13)^rotate(a,22))+((a&b)^(a&c)^(b&c));
        [a,b,c,d,e,f,g,h]=[(t+u)|0,a,b,c,(d+t)|0,e,f,g];
      }
      [a,b,c,d,e,f,g,h].forEach((x,j)=>state[j]=(state[j]+x)|0);
    }
    return state.map(x=>(x>>>0).toString(16).padStart(8,'0')).join('');
  }
  const candidates=[];
  for(const m of messages){
    const headers=m.payload?.headers||[];
    const header=n=>headers.filter(h=>h.name.toLowerCase()===n).map(h=>String(h.value));
    const times=Number(m.internal_date)/1000;
    if(!Number.isFinite(times)||times<expected.after_epoch||times>Math.min(Date.now()/1000,expected.before_epoch))continue;
    const rec=header('delivered-to').map(s=>s.trim().toLowerCase());
    if(rec.length!==1||sha256(rec[0])!==expected.recipient_sha256)continue;
    const subject=header('subject').join(' ');
    if(!/\b(?:verify|confirm|activate|verification|activation)\b/i.test(subject)||!/\b(?:e-?mail|account|registration|profile)\b/i.test(subject)||
       /password|security alert|reset|recovery|sign[ -]?in|login|change (?:your )?email/i.test(subject))continue;
    const auth=header('authentication-results').filter(h=>/^mx\.google\.com\s*;/i.test(h));
    if(auth.length!==1)continue;
    const aligned=auth[0].split(';').find(x=>/\bdmarc=pass\b/i.test(x))?.match(/header\.from=([\w.-]+)/i)?.[1]?.toLowerCase();
    if(header('from').length!==1)continue;
    const from=header('from')[0].match(/@([A-Za-z\d.-]+)>?\s*$/)?.[1]?.toLowerCase();
    const signer=auth[0].split(';').filter(x=>/\bdkim=pass\b/i.test(x)).map(x=>x.match(/header\.(?:d|i)=@?([\w.-]+)/i)?.[1]?.toLowerCase()).find(d=>d && from===aligned && (from===d || from.endsWith('.'+d) || d.endsWith('.'+from)));
    if(!signer||!m.id)continue;
    const htmlParts=[], plainParts=[];
    function walk(p) {
      if (!p.filename && typeof p.body?.content==='string') {
        if(p.mime_type==='text/html')htmlParts.push(p.body.content);
        if(p.mime_type==='text/plain')plainParts.push(p.body.content);
      }
      for(const child of p.parts||[])walk(child);
    }
    walk(m.payload);
    const links=new Set(), destinations=new Set();
    const target=href=>{
      const parsed=href.match(/^https:\/\/([a-z0-9.-]+)(\/[^\s\\#]*)?(?:#[^\s\\]*)?$/i);
      if(!parsed || parsed[1].toLowerCase()!==expected.portal || /password|reset|recover|(?:return|redirect|next)(?:_?url)?=/i.test(href))return false;
      const path=(parsed[2]||'/').split('?')[0];
      const parts=path.toLowerCase().split('/').filter(Boolean);
      if(/^[a-z]{2}-[a-z]{2}$/.test(parts[0]||''))parts.shift();
      const scope=expected.tenant.slice(expected.portal.length+1);
      if(!scope)return true;
      if(/successfactors|brassring/.test(expected.portal)){
        const tuples=((parsed[2]||'').split('?')[1]||'').split('&').map(v=>v.split('='));
        const key=expected.portal.includes('successfactors')?'company':'partnerid';
        return tuples.filter(([k])=>k.toLowerCase()===key).length===1 && tuples.some(([k,v])=>k.toLowerCase()===key&&String(v).toLowerCase()===scope);
      }
      if(expected.portal.endsWith('taleo.net')&&parts.includes('careersection'))return parts[parts.indexOf('careersection')+1]===scope;
      if(expected.portal.endsWith('oraclecloud.com')&&parts.includes('sites'))return parts[parts.indexOf('sites')+1]===scope;
      return parts[0]===scope;
    };
    for(const body of htmlParts)for(const a of body.matchAll(/<a\b[^>]*?\bhref\s*=\s*(["'])([^"']+)\1[^>]*>([\s\S]*?)<\/a>/gi)){
      const href=a[2].replace(/&amp;/g,'&'), label=a[3].replace(/<[^>]+>/g,' ').trim();
      if(!target(href))continue;
      destinations.add(href);
      if(/^(?:verify|confirm|activate)(?:\s+(?:my|your|the))?\s+(?:e-?mail(?: address)?|account|profile|registration)(?:\s+now)?$/i.test(label))links.add(href);
    }
    for(const body of plainParts)for(const a of body.matchAll(/https:\/\/[^\s<>"']+/gi))if(target(a[0]))destinations.add(a[0]);
    let method,action;
    if(links.size===1){ method='link';action=sha256([...links][0]); }
    else if(links.size===0 && expected.code_challenge_sha256 && destinations.size){
      const content=plainParts.length?plainParts.join(' '):htmlParts.map(v=>v.replace(/<[^>]+>/g,' ')).join(' ');
      const codes=new Set([...content.matchAll(/(?:verification|activation) code(?: is)?[\s:–-]*(\d{4,8})\b/gi)].map(m=>m[1]));
      if(codes.size!==1)continue;
      method='code';action=sha256('code|'+m.id+'|'+expected.code_challenge_sha256);
    } else continue;
    candidates.push({authenticated:true,authenticated_sender_domain:signer,recipient_sha256:expected.recipient_sha256,purpose:'account_verification',
      received_at:new Date(times*1000).toISOString(),message_id:m.id,destination_origin:'https://'+expected.portal,destination_tenant:expected.tenant,method,action_sha256:action});
  }
  return candidates.length===1?candidates[0]:null;
}
