let token: string | null = null
export function setToken(value:string|null){ token=value }
export async function api<T>(path:string, options:RequestInit={}):Promise<T>{
  const response=await fetch('/api/v1'+path,{...options,headers:{'Content-Type':'application/json',...(token?{Authorization:`Bearer ${token}`}:{}) ,...options.headers}})
  if(!response.ok){const error=await response.json().catch(()=>({detail:'Service unavailable'})); if(response.status===401) window.dispatchEvent(new Event('session-expired')); throw new Error(typeof error.detail==='string'?error.detail: error.detail?.map((e:{loc:string[];msg:string})=>`${e.loc.join('.')}: ${e.msg}`).join('; ')||'Request failed')}
  return response.status===204?undefined as T:response.json()
}
export const send=<T>(path:string,body:unknown,method='POST')=>api<T>(path,{method,body:JSON.stringify(body)})
export async function download(path:string,filename:string){const response=await fetch('/api/v1'+path,{headers:token?{Authorization:`Bearer ${token}`}:{}});if(!response.ok)throw new Error('Export failed');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url)}
