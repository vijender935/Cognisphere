import React from "react";
import {Send,Paperclip,X} from "lucide-react";
import FileUpload from "./FileUpload";

export default function Composer({text,setText,loading,send,stopStream,uploadFile,attachments,setAttachments,uploading}){
 return <div className="composer-wrap">
  <div className="composer">
   <FileUpload onFiles={uploadFile} disabled={loading||uploading}/>
   <textarea value={text} onChange={e=>setText(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}}} placeholder="Message your assistant..." disabled={loading}/>
   <button className="send" onClick={loading?stopStream:send} disabled={!loading&&!text.trim()&&!attachments.length}>{loading?<span className="spinner"/>:<Send size={18}/>}</button>
  </div>
  {(webSearch||memory)&&<div className="composer-tools">
   <button className={"tool-chip "+(webSearch?"on":"")} onClick={()=>setWebSearch(x=>!x)}><Globe size={14}/>Web search</button>
   <button className={"tool-chip "+(memory?"on":"")} onClick={()=>setMemory(x=>!x)}><Brain size={14}/>Memory</button>
   <button className="tool-chip" onClick={onConnectors}><Plug size={14}/>Connectors</button>
  </div>}
  {attachments.length>0&&<div className="attachments">{attachments.map(a=><span className="attachment" key={a.path}><Paperclip size={12}/>{a.name}<button onClick={()=>setAttachments(xs=>xs.filter(x=>x.path!==a.path))}><X size={12}/></button></span>)}</div>}
  {uploading&&<small className="uploading"><span className="mini-spinner"/>Uploading…</small>}
  <small>AI can make mistakes. Check important information.</small>
 </div>;
}