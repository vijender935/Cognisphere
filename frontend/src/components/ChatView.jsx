import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {Copy,Check} from "lucide-react";
import ProjectIcon from "./ProjectIcon";

export default function ChatView({chat,loading,copyMessage,copiedMessage,regenerate,editLastUser,setEditState}){
 const messages=chat?.messages||[];
 if(!messages.length)return <section className="messages"><div className="welcome"><div className="welcome-logo"><ProjectIcon size={25}/></div><h1>How can I help?</h1><p>Your personal AI assistant</p></div></section>;
 return <section className="messages">{messages.map((m,i)=><div key={i} className={"bubble "+m.role}><div className="role">{m.role==="user"?"You":"Assistant"}</div><div className="message-content">{m.role==="assistant"?(m.content?<ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>:loading&&i===messages.length-1?<div className="typing" aria-label="Assistant is thinking"><i></i><i></i><i></i></div>:null):m.content}</div><div className="message-actions"><button className="icon message-action" title="Copy message" onClick={()=>copyMessage(m.content,i)}>{copiedMessage===i?<Check size={14}/>:<Copy size={14}/>}</button>{i===messages.length-1&&m.role==="assistant"&&<button className="icon message-action" title="Regenerate response" onClick={regenerate}>↻</button>}{i===chat.messages.length-2&&m.role==="user"&&messages.at(-1)?.role==="assistant"&&<button className="icon message-action" title="Edit and resend" onClick={()=>{const edit=editLastUser();if(edit)setEditState(edit)}}>✎</button>}</div></div>)}</section>;
}