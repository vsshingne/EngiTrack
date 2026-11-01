import {useEffect,useRef,type ReactNode} from 'react'
import {X,LoaderCircle,Inbox,AlertCircle} from 'lucide-react'
export function Badge({value}:{value:string}){return <span className={`badge ${/Critical|Blocked|Delayed/.test(value)?'red':/Risk|High|Hold/.test(value)?'amber':/Completed|Healthy|Resolved/.test(value)?'green':/Progress|Review/.test(value)?'blue':'gray'}`}>{value}</span>}
export function Progress({value}:{value:number}){return <div className="progress-wrap"><div className="progress"><i style={{width:`${value}%`}}/></div><span>{value}%</span></div>}
export function Loading(){return <div className="state" role="status"><LoaderCircle className="spin"/> Loading delivery data…</div>}
export function ErrorState({error}:{error:Error}){return <div className="state error" role="alert"><AlertCircle/>{error.message}</div>}
export function Empty({text='No records yet. Create your first one to get started.'}:{text?:string}){return <div className="state"><Inbox/><span>{text}</span></div>}
export function Modal({title,children,onClose}:{title:string;children:ReactNode;onClose:()=>void}){const ref=useRef<HTMLDialogElement>(null);useEffect(()=>{const dialog=ref.current;dialog?.showModal();return()=>dialog?.close()},[]);return <dialog ref={ref} onCancel={onClose}><div className="modal-head"><h2>{title}</h2><button type="button" className="icon" aria-label="Close dialog" onClick={onClose}><X size={20}/></button></div>{children}</dialog>}
export function Pagination({page,total,size,onChange}:{page:number;total:number;size:number;onChange:(p:number)=>void}){return <div className="pagination"><span>{total} records · Page {page} of {Math.max(1,Math.ceil(total/size))}</span><button disabled={page===1} onClick={()=>onChange(page-1)}>Previous</button><button disabled={page*size>=total} onClick={()=>onChange(page+1)}>Next</button></div>}
export function Field({label,children}:{label:string;children:ReactNode}){return <label className="field"><span>{label}</span>{children}</label>}
export function Section({title,children,action}:{title:string;children:ReactNode;action?:ReactNode}){return <section className="panel"><div className="section-head"><h2>{title}</h2>{action}</div>{children}</section>}
export function dateText(value:string|null){return value?new Date(value.slice(0,10)+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'—'}
