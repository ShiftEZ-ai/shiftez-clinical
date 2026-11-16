/** Navigation uses the server's proved amendment target, never the issued session's obsolete clone ID. */
import type {PublishedOperationalAction} from './generated.js';
import {routeHash} from './routes.js';
import {notify} from './dom.js';

export function amendmentDestination(action:PublishedOperationalAction):{route:string;notice:string}|null{
  if(!action.amendment_workspace_id)return null;
  const shift=action.amendment_shift_state==='matched'?action.amendment_shift_id:null;
  const notice=shift?'':action.amendment_shift_state==='ambiguous'
    ?'The original duty cannot be matched uniquely. Choose the intended session in this amendment.'
    :action.amendment_shift_state==='changed_or_missing'
      ?'The original duty has changed or been removed. Choose a current session in this amendment.'
      :'Choose the intended session in this amendment.';
  return {route:routeHash('schedule',['week',action.date],{edit:action.amendment_workspace_id,...(shift?{shift,panel:'session'}: {})}).slice(1),notice};
}

export function openAmendment(action:PublishedOperationalAction,navigate:(route:string)=>void){
  const destination=amendmentDestination(action);if(!destination)return;
  navigate(destination.route);if(destination.notice)notify(destination.notice,'info');
}
