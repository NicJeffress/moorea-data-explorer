// The analyst starts closed on every page but the map (U2), using the app's own sidebar collapse: body.sidebar-collapsed sets
// --sidebar-width to 0 and slides the chat away, and the workspace follows. Phones keep their bottom sheet.
import {ask} from './field-charts.js?v=u7-3';
// The map's layer list lives in the sidebar (#sidebar-layers-pane), so closing it would take the layers away.
export const KEEPS_SIDEBAR=new Set(['map']);
export const closedByDefault=view=>!KEEPS_SIDEBAR.has(view);
export const NARROW_QUERY='(max-width:700px)';   // the width where the app's sidebar becomes a bottom sheet
// Keeps its state in a closure so it can be tested with a stand-in for the page.
export function analystPane(env){
 const collapsed=()=>env.body.classList.contains('sidebar-collapsed');
 const setCollapsed=on=>env.body.classList.toggle('sidebar-collapsed',on);
 let managed=false,prior=false,opened=false;
 const apply=()=>setCollapsed(!opened&&!env.narrow());
 return {
  // Call whenever the view changes. Reading the class first also picks up the app's own show and hide buttons.
  sync(view){
   if(closedByDefault(view)){
    if(!managed){prior=collapsed();managed=true;}
    else if(!env.narrow())opened=!collapsed();
    apply();
   }else if(managed){
    if(!env.narrow())opened=!collapsed();
    setCollapsed(prior);
    managed=false;
   }
  },
  open(){opened=true;if(managed)setCollapsed(false);},
  hide(){opened=false;if(managed)setCollapsed(true);},
  // The window crossed the phone width while on one of these pages.
  widthChanged(nowNarrow){
   if(!managed)return;
   if(nowNarrow)opened=!collapsed();
   apply();
  },
 };
}
let pane=null;
function browserPane(){
 if(pane)return pane;
 const query=matchMedia(NARROW_QUERY);
 pane=analystPane({body:document.body,narrow:()=>query.matches});
 query.addEventListener('change',e=>pane.widthChanged(e.matches));
 openOnFill(document,()=>pane.open());
 return pane;
}
// Every Ask button fills the chat box, on whichever page it sits, so a fill while the chat is closed opens it.
export function openOnFill(doc,open){
 doc.addEventListener('input',e=>{if(e.target?.id==='chat-input'&&doc.body.classList.contains('sidebar-collapsed'))open();});
}
// The app's bottom sheet opens itself to half height when it starts on a phone, leaving the page 342 of 812 px and showing an empty panel (docs/pr29-browser-review.md).
// The sheet belongs to the unmodified runtime library, so this steps it back the way its own handle does, once each time it starts. Choosing a tab still opens it.
export function settleSheet(doc=document,Observer=MutationObserver){
 const settle=()=>{if(doc.body.classList.contains('sheet-mode')&&doc.body.dataset.sheetDetent==='half')doc.getElementById('sheet-grabber')?.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown'}));};
 let was=false;
 const check=()=>{const now=doc.body.classList.contains('sheet-mode');if(now&&!was)settle();was=now;};
 new Observer(check).observe(doc.body,{attributes:true,attributeFilter:['class']});
 check();
 // Every Ask button fills the chat box, which a reader cannot do while its tab is closed. So a fill on a phone shows the chat, wherever it came from.
 doc.addEventListener('input',e=>{
  if(e.target?.id!=='chat-input'||!doc.body.classList.contains('sheet-mode'))return;
  if(doc.body.dataset.sheetTab==='chat'&&doc.body.dataset.sheetDetent!=='peek')return;
  doc.querySelector('#sheet-tabs [data-tab="chat"]')?.click();e.target.focus?.();
 });
}
export const syncAnalyst=view=>browserPane().sync(view);
export const openAnalyst=()=>browserPane().open();
export const hideAnalyst=()=>browserPane().hide();
// Ask opens the chat and fills in the question; Hide closes it and returns focus to Ask. CSS shows Hide only while the chat is open.
// resized lets a page with a map redraw it at the new width.
export function wireAnalyst(askButton,hideButton,question,resized=()=>{}){
 askButton.onclick=()=>{browserPane().open();resized();ask(question());};
 hideButton.onclick=()=>{browserPane().hide();resized();askButton.focus({preventScroll:true});};
}
