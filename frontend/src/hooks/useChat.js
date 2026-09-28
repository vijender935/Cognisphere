import {apiFetch} from "../api";
import {useEffect,useRef,useState} from "react";

export function responseDetailFromPayload(data,fallback){return data?.detail||fallback}
async function responseDetail(response,fallback){try{return responseDetailFromPayload(await response.json(),fallback)}catch{return fallback}}
export function getEditableLastUser(messages){if(!messages?.length||messages.length<2)return null;const userIndex=messages.length-2,old=messages[userIndex];return old?.role==="user"?{value:old.content,index:userIndex}:null}

const ACTIVE_JOB_STATUSES=new Set(["queued","running","retrying"]);

export default function useChat({API,chats,setChats,active,setActive,text,setText,attachments,setAttachments,loading,setLoading,notify,webSearch,memory,authenticated}){
 const sendingRef=useRef(false);
 const pollingRef=useRef(new Map());
 const mountedRef=useRef(true);
 const chat=chats.find(c=>c.id===active)||chats[0];

 function normalizeMessages(messages=[]){
   const result=[];
   for(const m of messages){
     if(result.length&&result.at(-1)?.role===m.role&&result.at(-1)?.content===m.content)continue;
     result.push(m)
   }
   return result
 }

 function update(messages){
   const normalized=normalizeMessages(messages);
   setChats(cs=>cs.map(c=>c.id===active?{
     ...c,
     messages:normalized,
     title:c.title==="New conversation"?(normalized.find(m=>m.role==="user")?.content||"New conversation").slice(0,32):c.title
   }:c));
 }

 function upsertPendingJob(job){
   if(!job?.session_id||!job?.user_message)return;
   setChats(cs=>{
     const exists=cs.some(c=>c.id===job.session_id);
     const base=exists?cs: [...cs,{id:job.session_id,title:job.user_message.slice(0,32)||"New conversation",messages:[]}];
     return base.map(c=>{
       if(c.id!==job.session_id)return c;
       const messages=c.messages||[];
       const already=messages.some(m=>m.role==="user"&&m.content===job.user_message);
       if(already)return c;
       return {...c,messages:[...messages,{role:"user",content:job.user_message},{role:"assistant",content:"⏳ "+(job.message||"Queued…")}]};
     });
   });
 }

 function applyJob(job){
   if(!job?.session_id||!job?.user_message)return;
   setChats(cs=>cs.map(c=>{
     if(c.id!==job.session_id)return c;
     const messages=[...(c.messages||[])];
     let userIndex=-1;
     for(let i=messages.length-1;i>=0;i--){if(messages[i].role==="user"&&messages[i].content===job.user_message){userIndex=i;break}}
     const assistantContent=job.status==="completed"?(job.result||"No response received."):job.status==="failed"?(job.error||"Background job failed."):("⏳ "+(job.message||"Processing…"));
     if(userIndex<0){
       messages.push({role:"user",content:job.user_message},{role:"assistant",content:assistantContent});
     }else if(messages[userIndex+1]?.role==="assistant"){
       messages[userIndex+1]={...messages[userIndex+1],content:assistantContent};
     }else{
       messages.splice(userIndex+1,0,{role:"assistant",content:assistantContent});
     }
     return {...c,messages,title:c.title==="New conversation"?job.user_message.slice(0,32):c.title};
   }));
 }

 async function pollJob(jobId,initialJob){
   if(pollingRef.current.has(jobId))return;
   pollingRef.current.set(jobId,true);
   let job=initialJob;
   try{
     for(let attempt=0;attempt<3600;attempt++){
       if(!mountedRef.current)break;
       if(attempt>0)await new Promise(resolve=>setTimeout(resolve,1500));
       try{
         const r=await apiFetch(API+"/api/v1/jobs/"+encodeURIComponent(jobId));
         if(!r.ok)throw new Error(await responseDetail(r,"Job status unavailable."));
         job=await r.json();
         applyJob(job);
         if(!ACTIVE_JOB_STATUSES.has(job.status)){
           if(job.status==="completed"||job.status==="failed"){
             if(job.session_id===active)setLoading(false);
             sendingRef.current=false;
           }
           return;
         }
       }catch(err){
         if(attempt===3599)throw err;
         // Temporary network loss must not cancel the server-side job.
       }
     }
   }catch{
     if(mountedRef.current)notify("Job status check failed; the server-side job may still be running.");
   }finally{
     pollingRef.current.delete(jobId);
   }
 }

 async function restorePendingJobs(){
   try{
     const r=await apiFetch(API+"/api/v1/jobs?status=queued,running,retrying&limit=100");
     if(!r.ok)return;
     const jobs=await r.json();
     for(const job of jobs){
       upsertPendingJob(job);
       pollJob(job.id,job);
     }
   }catch{}
 }

 useEffect(()=>{
   mountedRef.current=true;
   restorePendingJobs();
   return()=>{
     mountedRef.current=false;
   };
 },[API,authenticated]);

 async function regenerate(){
   if(loading||!chat?.messages?.length||chat.messages.at(-1)?.role!=="assistant")return;
   setLoading(true);
   try{
     const r=await apiFetch(API+"/api/v1/chats/"+encodeURIComponent(active)+"/regenerate",{method:"POST"});
     if(!r.ok){notify(await responseDetail(r,"Regeneration failed"));return}
     const d=await r.json();
     update([...chat.messages.slice(0,-1),{role:"assistant",content:d.answer||""}]);
   }catch{notify("Regeneration failed")}finally{setLoading(false)}
 }

 function editLastUser(){return loading?null:getEditableLastUser(chat?.messages)}

 async function send(){
   const message=text.trim();
   if(!message||loading||sendingRef.current||!chat)return;
   sendingRef.current=true;
   const next=[...chat.messages,{role:"user",content:message},{role:"assistant",content:"⏳ Queuing background job…"}];
   update(next);
   setText("");
   setLoading(true);
   try{
     const r=await apiFetch(API+"/api/v1/chat/jobs",{
       method:"POST",
       headers:{"Content-Type":"application/json"},
       body:JSON.stringify({
         message,
         session_id:active,
         attachment_paths:attachments.map(a=>a.path),
         web_search:webSearch,
         memory
       })
     });
     if(!r.ok){
       update([...next.slice(0,-1),{role:"user",content:message},{role:"assistant",content:await responseDetail(r,"Could not queue background job.") }]);
       sendingRef.current=false;
       setLoading(false);
       return;
     }
     const job=await r.json();
     applyJob({...job,user_message:message});
     pollJob(job.id,{...job,user_message:message});
   }catch(e){
     update([...next.slice(0,-1),{role:"user",content:message},{role:"assistant",content:e.message||"Could not queue background job."}]);
     sendingRef.current=false;
     setLoading(false);
   }finally{
     setAttachments([]);
   }
 }

 function stopStream(){
   if(!sendingRef.current)return;
   sendingRef.current=false;
   setLoading(false);
   notify("UI polling stopped. The job continues in the current server process.","success");
 }

 function newChat(){
   const id="web-"+Date.now();
   setChats(cs=>[{id,title:"New conversation",messages:[]},...cs]);
   setActive(id);
   setAttachments([]);
   if(window.innerWidth<701)window.dispatchEvent(new Event("chat:new"));
 }

 return {chat,update,regenerate,editLastUser,send,stopStream,newChat};
}
