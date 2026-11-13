// Loaded only by the release assembler. Local scientific inspection stays intact.
const note=document.createElement('aside');note.id='preview-notice';
note.innerHTML='<strong data-i18n="common.private">Private preview</strong><span data-i18n="copy.6fccdb735f72"> · Data are not for redistribution. </span><span class="preview-credit">MCR LTER · NSF</span> <details><summary data-i18n="ui.data_use">Data use & attribution</summary><p data-i18n="notice.credit">Data sets were provided by the Moorea Coral Reef Ecosystem LTER, funded by the US National Science Foundation (OCE-0417412). Source-specific credits, licences and scientific caveats remain with each view.</p><p data-i18n="notice.privacy">Questions, conversation history and selected query results are sent to OpenAI when you use the analyst. Do not enter private or sensitive information. The server stores usage totals, not full conversations. Your browser may retain chat history.</p><p data-i18n="notice.downloads">Original-file downloads and the standalone SQL preview are unavailable in this preview. Displayed values remain visible and can be saved; invitation-only access does not grant redistribution rights.</p></details>';
document.body.append(note);
// API enforcement is in PreviewBoundary; this removes misleading download controls.
const blocked=href=>/^\/api\/(gump-records\/source\/|ctd-records\/source\/|reef-temperature\/download\/)/.test(href)||/^\/api\/(materials|coral-multi|coral-photos)\//.test(href)&&/\/(download|original)$/.test(href);
function update(){
 for(const a of document.querySelectorAll('a[href]')){
  const href=a.getAttribute('href');
  if(blocked(href)||['/checks.html','/model-eval.html'].includes(href.split('?')[0]))a.setAttribute('data-preview-disabled','');
  else a.removeAttribute('data-preview-disabled');
 }
}
new MutationObserver(update).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['href']});update();
