import {watchJob,watchedJobs} from './api.js';
import {sameControl,type ControlKey} from './focus_key.js';
import {checkingWords,waitingWord,footerCheck,checkUnfinished,checkEndedStopped,rotaCheckCurrent,checkCancellable,checkRefused,checkTakeover,checkStoppedWords,CHECK_STOPPED_WHY,CHECK_TAKEN_OVER,takeOverJob,CHECK_NOT_STARTED,CHECK_AGAIN,checkBoundMs} from './rule_check_api.js';
import type {CheckWorker} from './rule_check_api.js';
import {recordError} from './feedback_context.js';
import {homePage,seeNotices,dutyButtons,reportedDuty} from './home.js';
import {quotaPanel, quotaUnits} from './quotas.js';
import {staffingDialog} from './staffing.js';
import {draftImpact} from './draft_impact.js';
import {configurationImpact,coverLines,type Impact,type Impacts} from './configuration_impact.js';
import {bidi,isolate,awaitAdoption,footerAct,duration,desktopNotice,helpButton,closeDialogs,brandEmblem,bootEmblem,brandRow,brandShort,envBadge,focusQuietly,markTag,inlineCheck,grants,type Permission,ruleFacts,phoneQuery,spanLabel,legend,statusLabel,pairWords,announce,compact,relabel,fitRow,trackStatusFooter,closeOrphanPopover,closePopover,adoptPopover,footerStatus,liftFooterStatus,alive,buildingPage,reportError,dropHoverCard,keepHover,enhanceTooltips,roleLabel,multiPick,plural,agree,setTypeColours,setRankColours,rankColour,setWeekStart,setWeekend,weekdayOptions,typeColour,icon,el,field,area,select,check,multi,button,note,badge,empty,emptyLine,sectionHead,notify,labelOf,table,showDialog,formDialog,confirmAction,leaveGuard,askDirtyDialog,dateUnits,formatDate,formatTime,formatDay,toLocal,iconButton,popover,menuItem,hoverCard,tagChip,colourOf,autoCode,uniqueCodes,codeRules,withIcon,routineReason,armed,saveBar,tabs,type IconName,type Child,type Field,type Choice,WEEKDAYS,weekOrder,weekdayNames,statusBadge,statusTone,historyDialog,type Tone,approvalPending as pendingApproval,readFailed,errorWords,releaseToasts,holdsLists,refused,notifyWaiting,notifyUndone,departmentToday,toastClause,type Legend,hasAccess,CAPABILITY,roleForCapability,avatar,factLine,disabledWhy,PAGE_WHY,failedNote} from './dom.js';
import {pillList,trackWidths} from './table_menu.js';
import {teamOrder,teamGroups} from './planner_teams.js';
import {teamView,teamProblem,rankOn} from './team_view.js';
import {dateRange,lastDayIncluded,shiftMonth} from './date_picker.js';
import {storedSetup,keepSetup,addTimeStep,addTimeRoom,scopeCapability,parseGroup,type RunSetup} from './autoschedule_setup.js';
import {peopleToSchedule,peoplePopoverKey} from './schedule_group.js';
import {rememberedAccount,rememberAccount,forgetAccount,signingInAs,forgetDeviceMemory} from './account_memory.js';
import {applyTheme,setTheme,theme,isDark,motionStill,setMotionStill,navPinned,setNavPinned,clearPrefs,nameMode,setNameMode,type Theme,type NameMode,setDepartmentNames,ownNameMode} from './theme.js';
import {openPalette,compactSearch,type PaletteItem} from './palette.js';
import {exportDialog,type ExportSource} from './schedule_export.js';
import {scheduleGrid} from './schedule_grid.js';
import {DIMENSIONS,TIME_AXES,isDim,personDim,dimOf,type Dim,type Column as GridColumn} from './schedule_dimensions.js';
import {SCHED_VIEWS,VIEW_RANGE,RANGE_NAMES,LIVE,actsOn,defaultRotaStates,footerIsPublished,periodFor,weekOf,linkMoved,stepAnchor,periodLabel,scheduleNav,layered,calendarDays,dayTimeline,overlapChoice,type SchedView,type SchedRange,type LayerMode,type Period,type ScheduleWindow,type Overlaps,type OverlapValue} from './schedule.js';
import {calendarPeriod,addDays,filterShifts,isClosed,closureText,absentFrom,grantOverlap,membershipCovers,LAYER_DEFAULTS,savedLayers,type PlanRange} from './planner_logic.js';
import {normalSearch,recordMatcher,fieldMatcher,searchParam,serverCapability,type SearchOptions} from './search.js';
/** The Schedule's session search options (search.ts), saved with the planner view (search_options). */
let schedSearch:SearchOptions|undefined;
/** What a person picked in the search shows on the Schedule (PlannerViewSettings.person_display; display only): the
 *  person, their recorded supervision team, or everyone on their duties (the default). Needs a picked person, never
 *  guessed from typed text. */
let personDisplay:'person'|'team'|'duty'=PLANNER_PERSON_DISPLAY_DEFAULT;
/** The people a person display is about: a person picked in the search, and the people the Filter keeps by its plain
 *  "Person is any of" rows (positive and explicit). A Person condition inside an Or group or with "is not" is not
 *  guessed at: the display then stays on everyone. Typed text never counts. */
const pickedPeople=(o:SearchOptions|undefined,filters:Record<string,string[]>|null|undefined,conditions?:Conditions|null):string[]=>{
  const ref=normalSearch(o)?.reference,out=new Set<string>();
  if(ref?.kind==='people')out.add(ref.id);
  if(!conditions||!conditionsMention(conditions,'person_ids'))for(const id of filters?.person_ids||[])out.add(id);
  return [...out];};
const personPicked=(o:SearchOptions|undefined)=>pickedPeople(o,schedFilters,schedConditions).length>0;
/** The Schedule toolbar's refresh: a grid's resized row headings, days or cells belong to the view, so its saved-view
 *  button shows the change (set by each Schedule render; the grips exist from the first draw). */
let scheduleSync:(()=>void)|null=null;
import {clashNote,cancelButtons,setClashRecheck,type Clash} from './double_booking.js';
import {personPill} from './person_pill.js';
import {dutyColour,teamLayout,openSession,coverOf,sessionSummary,assignCandidate,inlineFill,removeAssignment,toggleHelp,helpOpen,closeWeekPanel,stopAsking,queueFill,type FillIssue,weekView,collapsed,shownGroups,SESSION_GROUPS,isSessionGroup,weekLayoutNow,plannerPanels,resolveQueue,startResolve,plannerSessions,type WeekHooks,type MenuTarget,type MenuEntry} from './planner_week.js';
import {propertiesOf,setDepartmentProperties,itemParts,itemHead,sessionCode,ITEM_DRAWS,type ItemSurface} from './schedule_item.js';
import {setupGuidePage,setupSteps,dataHealth,missingSetup,stepDone,setupCounts,type RankCodeClash} from './setup_guide.js';
/** SW3-D-11: the last read of ranks sharing a short code (Data health and its nav count). */
let rankCodeClashes:RankCodeClash[]=[];
import {staffImportSection} from './staff_import.js';
import {keptReads,forgetReads,probeWorkers,workersKnown,reprobeWorkers,serverChecking,oidcDepartments,oidcChoose,oidcCancel,get,rotaMeta,rotaWhole,request,newest,background,leavePage,onUndone,undoInPlace,applyUndone,type Undone,command,READ_ONLY_COMMANDS,takeUndo,login,logout,instant,download,clearSessionState,readAfresh,ApiError,sessionChanged,sessionKey,savingNow,signingOutNow,readsDone,WITHDRAWN,knownRevision,onSessionReset,awaitingApproval,withdrawApprovals,identify,refreshSession,guardDraftEdits,awaitJob,ROTA_DECISIONS,redoDraft,undoRefused,serverHealth,automatic,ownSavingNow,confirmingNow,savingWords,updatingNow,UPDATING_WORDS,openingPage,applyWrote,APPLY_CONFIRMED} from './api.js';
import {brand,applyBranding,parseBranding,documentTitle} from './brand.js';
import {brandingPanel} from './branding_settings.js';
import {bulkSelection,setBulkContext,registry,editRoles,type BulkAction,type SelectionValue} from './bulk.js';
import {relatedPage,refreshRelated} from './bulk_children.js';
import {setRuleNumber} from './bulk_rule.js';
import {bulkEditorPage,setSheetContext,sheetStagedCount} from './sheet.js';
import {runBulk,openChange,openStaged,type BulkChange} from './bulk_change.js';
import {SHOW_AFTER,READ_TIMEOUT,busyLabel,joinRun,WAIT_WORDS,flight,skeleton,working,settle,reveal,track,busy,fadeOut,topProgress,swap,delayed,elapsed,observeSize,say,type Shape} from './loading.js';
import {stoppedWords,causeOf,causeWords} from './job_state_api.js';
import {selectionBar} from './selection_bar.js';
import {sidePanel,forgetPanelWidths,dockPanel,closeDock,dockDirty,dockLeave,dockOutlived,dockRevision,keepDock,markOpen,rowMark,type Dock,type PanelPosition} from './side_panel.js';
import {dataTable,conditionFilter,joinFilter,mentions as conditionsMention,holdsForSomeone,tableFilters,tableAsOf,tableSearch,tableSearchOptions,tableServerConditions,openFiltered,clearTableViews,restoreTableViews,holdTableState,tableColumns,sameColumns,setTableColumns,type TableColumns,captureTableScroll,restoreTableScroll,setTableAccount,fetchViews,savedViewItems,viewActions,collectionToolbar,optionMenu,openingView,type SortValue,type ToolbarSpec,type SavedView,type EditSpec,type RowAction,type Column,type SortKey} from './table_view.js';
import {peopleView,refreshPeople,findsPerson,PEOPLE_GROUPS,PEOPLE_SORTS} from './planner_people.js';
import {RULE_KINDS,RULE_NUMBER_FIELD,RULE_TOPICS,ruleTopic,LINE_KINDS,lineField,lineLabel,lineWho,isOrder,perPersonEditor,SETTING_LABELS,MODE_LABELS,lineSetting,lineHasNumber,protectTimeForm,protectedTimeRule,calendarProtectionPanel,catalogueEntryOf,ruleConditionFields,activeOn,peopleChoices,colourSwatches,catalogueForm,workspaceForm,shiftForm,sessionsHeld,relationshipForm,programmeEligibilityForm,availabilityForm,availabilityFields,leaveTypePicker,LEAVE_RECORD_KINDS,policyForm,quotaLimits,quotaNumberLabel,exceptionForm,safetyRule,allowsException,staffNeeded,type Context} from './forms.js';
import {recurringSessions,seriesGroups} from './recurring_sessions.js';
import type {Me,Row,PersonRow,Catalogues,WorkspaceData,Evaluation,Finding,PublicData,PlannerSession} from './types.js';
import {fillOptions} from './candidates.js';
import {applyEditDelta,applyCheckDelta,withoutOwnCheck,remoteEdit} from './edit_delta.js';
import {baselineDriftKey,noteDriftInputs,applyDriftInputs} from './baseline_drift.js';
import {drawnBase} from './settings_base.js';
import {mayDecide} from './independence.js';
import {routingPage,reviewPanel,approvalDialog,decidesStep,notDecider,SWAP_KIND_WORDS} from './governance.js';
import {requestsPage} from './requests.js';
import {leaveAllowanceSettings} from './leave_balances.js';
import {personProfilePanels,personSections,personTabs,profileSection,profileRow,type ProfileParts} from './person_profile.js';
import {bulkImpactDialog,comparePanel,scheduleView} from './planner_tools.js';
import {mountRules,selectTicked,openRotaRules,openRotaRulesFor,unpin,pin as pinRules,highlightRule,dayTitle,ROTA_TARGETS,loadRuleMap,type Target as RuleTarget,type RuleMap,type MapRule} from './planner_rules.js';
import {generationOptionsFields,fairnessPlateauField,timeField,stopFields,stopsDefault,stopWords,generationProgress,stageLabel,generationPhaseTimings,PRESETS,DEFAULT_PRESET,knownPreset,presetOptions,weightSummary,animationFields,animationSettings,whileFresh,ownedListener,liveFinisher,LIVE_STEPS,type AnimationSettings} from './generation_options.js';
import {ruleReview} from './rule_review.js';
import {publishedAbsenceLabel,plainCode,openPlaceIssue,placeReason,type PlaceFix,leaveLabel,leaveDates,setLeaveDisplay,setLeaveTypes,issueKind,findingNumbers,alsoShort,LEAVE_DISPLAY_CHOICES,setFindingNamer,causeIssue,causeText,causeWhat,draftComparison,sinceOfMany,mergeCauseGroups,runError} from './findings.js';
import {issuesPage,issueKey,type IssuesHooks} from './issues.js';
import {setIssueNav,issueList,sessionChips,dayLabels,findingIssue as findingItem,type Issue,type SessionChip} from './issue_marker.js';
import {ASSIGN_KINDS,actionLabel,deltaWords,fixListPage,fixPanelNodes,lastFixList,applyFixDelta,fixDeltaOf,fixPills,fixGate,fixRow,fixLevel,mustWords,couldWords,reviewItemsOf,reviewedWithRota,fixSummaryOf,readFixList,heldFixList,type FixCause,type FixHooks,type FixList,type FixSummary,type FixStep} from './fix_list.js';
import {scoreReview} from './score_review.js';
import {outcome,breaksNote,runFacts,resultHead,strictlyCurrent,candidateOptimality,readyWords,scoreTile,tile as statTile,scoreChange as statChange,stillToFix,changesByCause,clashCard,isClash,inputsChanged,rechecked,drifted,driftNote,nothingApplied,applyNeedsReview,APPLY_NOT_CONFIRMED,NOTHING_APPLIED,adjustForRotaDialog,solverReceipt,outsideGroupLine} from './autoschedule_result.js';
import {scoreCheckPage,scoreCheckTitle,type Step as ScoreStep} from './score_check.js';
import {aiProviderPage} from './ai_provider.js';
import {startWorker,updateWorker,installWay,install,setBadge,tabAlertsOn,signedOut,reconcile,forgetPushStatus,alertNotification} from './pwa.js';
import {notificationsPanel,notificationPolicyPage,unreadCount} from './notifications.js';
import {autoSameSessions} from './scoreCheckApi.js';
import {allLimits,levelBadge,limitPill,numbersEl,copyNumbers,numbersBlock,perWords,whoShort,whereShort,whoWhen,healthStrip,plainLines,exceptionList,exceptionsEditor,ruleSentence as sentenceOf,ruleDetails,quotaAmount,quotaPeriod,lineSessions,orderedLines} from './rule_view.js';
import {scorePill,scoreWhy,shownScore,scoreDelta} from './score.js';
import {fairnessPage,resetFairness,restoreFairnessViews,nothingPublished} from './fairness.js';
import {wishesPanel,wishTypeSettingsFields} from './wishes.js';
import {conditionBuilder,conditionsText,conditionsSummary,lineMatches,treeMatches,scopeAsConditions,isTyped,typedFields,literalValues,negativeOp,forgetResolved,RULE_FIELDS} from './conditions.js';
import {dateSetColumns,dateSetDates,entrySummary,importDateSet,copyDateSet,loadUpcoming} from './date_sets.js';
import {candidateReview,diagnosticContext} from './candidate_review.js';
import type {ViewProperty,LiveChange,LiveScope,WorkspacePartPage,SolverExclusionCount,AuthConfigStatus,FindingObservationSummary,JobRunContext,PublishedOperationalSummary,PublishedOperationalWarning,JobRecovery,FindingCauseGroup,FindingFixHint,RotaSummary,SessionSummary,GenerationOptions,PlannerViewDefinition,PlannerViewSettings,PersonLabel,Condition,Conditions,PersonAmount,RotaCauseSummary,NotificationList,VisibleContact} from './generated.js';
import {personLabel,personLink,labelPart,useLabel,useNames,onPersonOpen,personName,nameText,shortCode,labelFacts,displayName,forgetNames,fullNameOf} from './person_label.js';
import {personCard} from './person_card.js';
import {CONTACT_KINDS,contactAudience} from './contacts.js';
import {viewSettings,segment,labelBody,type ViewSettingsSpec} from './view_settings.js';
import {WEB_COMMIT} from './build.js';
import {RUN_SUPERSEDED,AUDIT_ACTION_CATALOGUE,RULE_CATALOGUE_V2,SCORE_WEIGHT_LABELS,STRENGTH_LABELS,WHEREABOUTS_DEFAULTS,WHEREABOUTS_LIMITS,HONORIFIC_DEFAULTS,NAME_PARTICLE_DEFAULTS,NAME_LABEL_DEFAULTS,PLANNER_COLOUR_LABELS,PLANNER_COLOUR_TAG_LIMIT,PLANNER_PERSON_DISPLAYS,PLANNER_PERSON_DISPLAY_DEFAULT,PLANNER_TEAM_ROWS_DEFAULT,PLANNER_WIDTH_KEYS,AUTO_SCHEDULE_LIMITS,GENERATION_CONTROL_DEFAULTS,GENERATION_SEARCH_FINISHED_STAGES,BULK_SETTING_FIELDS,APP_VERSION,ROLE_NAMES,INSTALLATION_ADMIN_ROLE,LINE_SETTINGS,ANY_LINE_SETTINGS,ENDED_REASON,RULE_NUMBERS,SWAP_LIMITS,SWAP_KINDS,EVENT_HORIZON_MAX_DAYS,MAX_APPROVALS_REQUIRED} from './generated.js';
import {upcoming} from './agenda.js';
import {reloadFor,justUpdated,serverBuild,noticeServerBuild,staleHooks,updateAfter} from './stale_bundle.js';
import {schedulingGroupsPage,NO_ROTA_COORDINATORS} from './scheduling_groups.js';
import {planningOf,groupRow,type PlanningContext,type GroupState} from './groups_view.js';
import {DRAFT_GROUPS,draftGroup,draftGlyph,draftHint,editableDraft,sandboxPill,rotaFor,newPrivateDraft,shareDraft,designateDraft,rotaCoordinators,mergePanel} from './drafts.js';
import {lifecycleItem,lifecycleOf,archivedRotas,restoreRota} from './rota_lifecycle.js';
import {CONFIG_KINDS,CONFIG_GROUPS,kindLabel,singularOf,purposeOf,aboutOf,effectOf,createLabel,addressOf,kindAt,isSettings} from './config_kinds.js';
import {TERMS,PARTICIPATION,onlyRotaCoordinator,unlinkedStaff} from './terms.js';
import {LEVEL_WORDS} from './issue_marker.js';
import {tagsPage,tagEditor,tagLabel,tagRefs,tagsFor} from './tags.js';
import {PAGE_TABS,parseRoute,routeHash,ruleNotice,withoutPrototype} from './routes.js';
import {grantRefusal} from './role_grants.js';
import {integrationsPage} from './integrations.js';
import {signInConfigPage} from './sign_in_config.js';
import {readinessRegion,requirementEvidence,currency,notCompared,SUPERSEDED,type Currency} from './planning_context.js';
import {openAmendment} from './published_amendment.js';
import {publishedSession,changesChip,markChanges,loadChanges,forgetChanges,changeable,replaceNext,stagedCoverage,changedSessions,markDiff,type ChangeDiff,type PublishedHooks} from './published_changes.js';
import {ruleCataloguePage,readRulesAhead,relaxForPerson,promoteAdjustment,namer,countWords,numbersText,daysText,safetyRetirement,SAFETY_RETIREMENT_NOTICE} from './rule_catalogue.js';
import {simplifyPanel,conversions,conversion} from './rule_simplify.js';
import {restoreRulesViews,restoreRulesScroll} from './rules_tree.js';
import {enhanceSelects,forgetRecent,openPicker} from './picker.js';
import {accountLinkPage,changePasswordDialog,linkDialog,inviteDialog,onboardingPanel,sessionsPanel} from './accounts.js';
import {installFeedback,openFeedback,feedbackPage,SHORTCUT as FEEDBACK_KEY} from './feedback.js';
import {rotationBoard} from './rotation_board.js';
import {peopleTimeline,timelineAvailable} from './person_timeline.js';
import {menu,menuSource,opens} from './dom.js';  // the shared menu (UX › controls S2b)
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {byLabel} from './dom.js';  // the one name order of every list of people
import {overflowMore,type MoreNoun} from './dom.js';  // the one "+N" (DESIGN D8)
import {SAFETY_DECIDED,UNDECIDED,baselineSourceLabel,changeLabel,refreshPlan} from './draft_baseline.js';
import {groupToggle} from './dom.js';  // a group's header is its toggle (UX › controls S5)
import {tabs as sharedTabs} from './dom.js';  // the one tab set (UX › controls S8)
import {moreWords} from './dom.js';  // the one "+N" and its words (UX › controls S9)
import {MESSAGES_KEY} from './dom.js';  // the messages' key (issue 1408)
import {panelSearch} from './dom.js';  // the one search box in a panel (UX › controls S10)
import {couldntLoad} from './dom.js';  // a failed read's words (DL-045)
import {notYet} from './dom.js';  // a control held in place until it can be used (D59)
import {rotaOutdated} from './dom.js';  // one rota verdict

applyTheme();
const root=document.querySelector('#app')!;
let me:Me|null=null,catalogues:Catalogues={people:[]},view='home',workspaceId='',publicationId='',catalogueKind='assignment_types',planMode='week',searchText='';
/** The Schedule's filters, saved in a view as `filters` (the shared collection-filter shape): each field keeps any of
 *  its values; a session shows when it matches every field. `person_ids` also comes from a ?person= link. */
let schedFilters:Record<string,string[]>={};
// The Filter's other lines (Or, is not, groups): the shared condition builder's tree beside the plain rows (SR-06).
let schedConditions:Conditions|null=null;
const onePerson=()=>schedFilters.person_ids?.length===1?schedFilters.person_ids[0]:'';
let workspaceListOffset=0;
// Schedule: the view, its anchor day (the timeline is continuous, so a period is found from a day, not a rota page), the
// drafts layer (edit one, side by side with `compareId`, or overlay), My duties, and a superseded version shown alone.
// `viewRange`: a range chosen for People, Coverage, List or the Week calendar ('' = the view's own); `viewNames`: how the
// view names people ('' = the Names default). Both are saved with a view.
let viewRange:SchedRange|''='',viewNames:PersonLabel|null=null;
// The view's Days & time: weekdays shown (Monday 0 … Sunday 6; null = every day), its own week start, the hours the Day
// and Timeline layouts show, and the length of a Custom range.
const DAYS_DEFAULT={weekdays:null as number[]|null,week_start:null as number|null,time_window:null as {start:string;end:string}|null,days:14};
let viewDays={...DAYS_DEFAULT};
let schedView:SchedView='week',planDay='',planDayFromRoute=false,pickedDay='',layerMode:LayerMode=LAYER_DEFAULTS.mode,compareId='',mineOnly=LAYER_DEFAULTS.mine,draftNone=false,
  /** The Draft layer as chosen (Layers › No draft, or a saved view), not a link or a day without drafts. */draftsShown=LAYER_DEFAULTS.draft,jumpToDraft=false,sourcePublication='',routeShift='';
/** D25 (maintenance, SX-02): the rota an approver opened for review from Home (?approve=); on a phone it opens as on a desktop. */
let phoneReview='';
// The open publication version and its live coverage, reused by report duty lists.
// Keyed like the server's coverage cache: a new version, a change of current version, or new rules, people or availability load them again.
let publishedCache:{key:string;data:Promise<PublicData>;coverage?:Promise<Row>;evaluation?:Evaluation}|null=null;
function publicationCache(id:string,current:unknown){
  const org=me!.organisation,key=[id,current,org.input_revision,org.availability_revision].join('|');
  if(publishedCache?.key!==key){const c:NonNullable<typeof publishedCache>={key,data:get<PublicData>('/api/publications/'+id)};c.data.catch(()=>{if(publishedCache===c)publishedCache=null;});publishedCache=c;}
  return publishedCache;
}
// A planner view is a layout (planMode: week = the calendar grid, person, coverage, list) over a range of days (planRange).
let planRange:PlanRange='week',plannerRota:Row|null=null;
const lastView={key:'',day:''};
// Workload targets of the open rota (shown in 'Change rules & targets for this rota').
let plannerTargets:(()=>HTMLElement|null)|null=null;
/** What Export copies: the Schedule view on screen, its name and period (set by each Schedule render). */
let scheduleExport:(()=>ExportSource)|null=null,scheduleSettings:((close:()=>void)=>ViewSettingsSpec)|null=null,exportRota:{starts_on:string;ends_on:string}|null=null;
// The auto-schedule panel's per-rota state: its step, what to fill and the run's priorities.
const freshScheduling=(workspace='',open=false)=>({workspace,open,scope:'period',keep:undefined as boolean|undefined,leaveRoom:undefined as boolean|undefined,
  minutes:undefined as number|undefined,fairness:undefined as number|undefined,stops:undefined as Partial<GenerationOptions>|undefined,resume:undefined as {job:string;minutes:string}|undefined,step:0 as 0|1|2|3,seen:'',fresh:'',from:'',to:'',clinics:[] as string[],preset:'',
  /** People to schedule (null: everyone); pickGroup: a group must be chosen before Run (Schedule another group…);
   *  groupKeep: a group run's own Keep existing (on unless unticked); scopeError: the server's reason a group was refused. */
  group:null as Conditions|null,pickGroup:false,groupKeep:undefined as boolean|undefined,scopeError:'',
  /** The run the grid is showing live (autoschedule_live.ts) until it ends. */live:'',
  /** A Run press waiting for the server (its kind): no second run starts meanwhile, even from a redrawn panel. */starting:'',
  /** The setup's fields are open (Change): a redraw keeps them open. Not kept on the device (keepSetup). */editing:false});
let schedulingSettings=freshScheduling();
// Each rota keeps its own run setup (dates, clinics, priorities) while you switch between rotas; sign-out forgets them.
const schedulingByRota=new Map<string,ReturnType<typeof freshScheduling>>();
// #1041: the one Approve review opening per rota (the ?approve= route or a press): a redraw meanwhile holds its button.
const reviewRuns=new Map<string,Promise<unknown>>();
// The intended run setup is also kept on this device (autoschedule_setup.ts), so a reload keeps it; runs are the server's.
const ownSetup=(workspace:string)=>storedSetup(me?.identity_id||'',me?.department.id||'',workspace) as Partial<ReturnType<typeof freshScheduling>>;
const keptSetup=(s:ReturnType<typeof freshScheduling>)=>{if(s.workspace&&me)keepSetup(me.identity_id,me.department.id,s.workspace,s as RunSetup);};
// A rota just created with "Run auto-schedule after setup" opens with its Auto-schedule panel on Review & apply, which
// shows the run's progress and then its result, so the person who asked for it never meets a silent, unstaffed draft (G-30).
window.addEventListener('rota-auto-schedule',e=>{const id=String((e as CustomEvent).detail?.workspace||'');if(!id)return;
  const s={...freshScheduling(id,true),step:3 as const};schedulingByRota.set(id,s);schedulingSettings=s;});
// A view change (tab, layout, filter, search, grouping, View settings…) redraws the planner from the data it already has,
// however long the page has been open: no requests. Freshness is the live pulse's and the saves' job (forgetSchedule):
// a two-minute age limit here re-read the rota list, the draft, its jobs and windows on a filter change (maintenance, maintenance).
// The newest render decides (reuseFor = its serial): two redraws asked at once (a control announcing one change twice)
// both draw from memory; the flag left for the second was consumed by the first, which then read everything (maintenance, 444ac3e).
let plannerMemo:{id:string;offset:number;page:{workspaces:Row[];total:number;has_more:boolean};views:SavedView<PlannerSaved>[]}|null=null,reusePlanner=false,reuseFor=0;
function redraw(){reusePlanner=view==='schedule';return renderPage();}
/** The rota and revision your own Undo left (its reply): Redo is offered there until the rota changes again (H-11). */
let redoAfter:{id:string;revision:number}|null=null;
// Calendar renderers differ on phones. Reuse the loaded rota when the breakpoint changes; the renderer keeps an
// explicit Agenda/Grid choice, and the existing redraw path preserves docked forms and their unsaved values.
// With the phone scope on, crossing the breakpoint changes the navigation and maybe the page: the address is read again
// and the shell and page are drawn afresh.
phoneQuery.addEventListener('change',()=>{if(!me)return;
  // The phone menu makes the page behind it inert: widening past the phone width closes it, or the page stays unusable.
  if(!phoneQuery.matches)frame?.drawer(false);
  // Same address (only the width changed): parse it again without re-applying a link's resets (search, filters, view).
  if(phoneScope()){if(location.hash===routeSeen)setRoute(location.hash.length>1?location.hash:view);else readRoute(view);renderedView='';void redraw();return;}
  if(view==='schedule'&&!schedSubPage()&&['week','month','day'].includes(schedView))void redraw();});

/** Phone scope reviewed: on a phone Rota is for employees. A page opens there
 *  only if its `phone` test (paths) passes; any other page shows "Best on a desktop", whose "Continue anyway" opens it as on
 *  a desktop for the rest of the browser session. ONE switch: false gives every page on phones exactly as before, with no
 *  other change. A '*' in the session list does the same (the browser tests' default, so phone layouts stay tested). */
const PHONE_DESKTOP_NOTICE=true;
const PHONE_CONTINUE='fmuc.phone-continue';
const continued=new Set<string>((()=>{try{return JSON.parse(sessionStorage.getItem(PHONE_CONTINUE)||'[]') as string[];}catch{return [];}})());
const continueOnPhone=(id:string)=>{continued.add(id);try{sessionStorage.setItem(PHONE_CONTINUE,JSON.stringify([...continued]));}catch{/* kept for this visit */}};
/** Signing out leaves nothing in browser storage: the choices end with the session ('*', the tests' bypass, stays for this page). */
const forgetPhoneContinue=()=>{for(const id of continued)if(id!=='*')continued.delete(id);try{sessionStorage.removeItem(PHONE_CONTINUE);}catch{/* nothing stored */}};
const phoneScope=()=>PHONE_DESKTOP_NOTICE&&!continued.has('*');
/** This page is drawn for a phone now: its employee version (Home, Schedule), or the notice when it has none. */
const phoneLite=(id=view)=>phoneScope()&&phoneQuery.matches&&!continued.has(id);
const fitsPhone=(id:string,parts:string[]=[],query=new URLSearchParams())=>!!paths[id]?.phone?.(parts,query);
/** Navigation and search (pages, commands, results) offer only the phone pages on a phone, even after "Continue anyway". */
const desktopNow=()=>!phoneScope()||!phoneQuery.matches;
const phoneNav=(id:string)=>desktopNow()||fitsPhone(id);
const phoneBannerOff=new Set<string>();
// Drafts and published windows already read this visit: a view change, period move or search reuses them (no request);
// any save, refresh or live update (the pulse moved) reads afresh. Bounded: windows past 24 are dropped oldest first.
// `source`: the one publication a ?source= view shows, reused by a redraw (G-01: a coordinator's publication read is
// megabytes and was read again by every redraw, e.g. once its change list arrived); any refresh reads it again.
const scheduleCache={drafts:new Map<string,WorkspaceData>(),windows:new Map<string,Promise<unknown>>(),source:null as {id:string;read:Promise<PublicData>}|null};
let draftReadIn='';
let replaceMisses=0,routePerson='';
const forgetSchedule=()=>{plannerMemo=null;scheduleCache.drafts.clear();scheduleCache.windows.clear();scheduleCache.source=null;scheduleEpoch++;};
// The first open of a rota draws its days in view from its compact detail and that window's sessions and findings (not
// the rota's whole detail of megabytes), then reads the whole rota at once. Until it lands the week is read-only (D59):
// it is read, paged and opened read-only; every write is in place but held with its reason (dom.ts notYet), and nothing
// claims a whole-rota fact (footer health, score, Fix list; its check marks say they are the last check's). No
// whole-rota feature (footer counts, checks, bulk, swaps, hover facts, publishing) ever runs on the window.
// The window is never put in scheduleCache.drafts, which every reader takes for the whole rota.

let scheduleEpoch=0,windowOnScreen=false;  // a window drawn, the whole rota still loading: the page is not loaded yet
/** Why a write waits while only the window is in (its tip and description, and the footer's busy pill). */
const WHOLE_LOADING='Loading the whole rota…';
const wholeSeen=new Set<string>(),wholeReads=new Map<string,{epoch:number;read:Promise<WorkspaceData>}>();
/** The rota's whole detail, read once at a time and kept only if nothing was forgotten meanwhile (a live change). */
/** `quiet`: read behind a drawn window (D59), never on the top bar. `seen`: false for a read started ahead (readAhead),
 *  which leaves the Planner drawing its window first. `reader`: how it is read (at boot, an app read). */
function wholeRota(id:string,quiet=false,seen=true,reader:(path:string)=>Promise<WorkspaceData>=path=>get<WorkspaceData>(path,{quiet})):Promise<WorkspaceData>{
  const epoch=scheduleEpoch,going=wholeReads.get(id);if(seen)wholeSeen.add(id);if(going?.epoch===epoch)return going.read;
  // A read that started before an edit lands after its delta was applied: the newer copy held stays.
  const read=rotaWhole(id,reader).then(d=>{const held=scheduleCache.drafts.get(id);
    if(held&&!held.partial&&Number(held.workspace.revision)>Number(d.workspace.revision))return held;
    if(epoch===scheduleEpoch){scheduleCache.drafts.set(id,d);draftReadIn=schedView;}return d;})
    .finally(()=>{if(wholeReads.get(id)?.read===read)wholeReads.delete(id);});
  wholeReads.set(id,{epoch,read});return read;
}
const partial=(d:WorkspaceData|null|undefined)=>!!d?.partial;
/** The draft as held now (an edit's delta patches it in place): what a name's menu, lock or exchange acts on, never the
 *  copy captured when the grid was drawn. */
const heldDraft=(d:WorkspaceData)=>{const now=scheduleCache.drafts.get(String(d.workspace.id));return now&&!now.partial?now:d;};
/** FE-PERF: a Planner address naming a rota (?edit=) starts that rota's reads side by side, not one after another
 *  (rota list → compact detail → window → published sessions → whole detail measured 5 deep, 4.3 s on December): its
 *  compact detail, its whole detail (the slowest read, megabytes; joined by wholeRota) and, for a dated address, the
 *  published sessions of the days in view, and at boot the rota list too. Started at boot beside the lists (`background`: the first page change must
 *  not abort them) and on opening the Schedule beside the rota list; schedulePage takes what matches, once. */
type RotaList={workspaces:Row[];total:number;has_more:boolean};
let ahead:{id:string;epoch:number;list:Promise<RotaList>|null;meta:Promise<Awaited<ReturnType<typeof rotaMeta>>>;range:string;window:Promise<ScheduleWindow>|null}|null=null;
function readAhead(boot=false){
  // At boot an app read (the first page change must not abort it); on opening the Schedule a page read, so a kept reply
  // (keptReads) answers it at once.
  const reader:typeof get=boot?background:get;
  const id=routeQuery.get('edit')||'';ahead=null;
  if(view!=='schedule'||schedSubPage()||!id||id==='none'||wholeSeen.has(id)||!permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)||phoneLite())return;
  const dated=planDayFromRoute&&schedView!=='year'&&layerMode!=='side'&&!sourcePublication;
  const days=dated?periodNow(planDay,me!.organisation.week_start).days:null,range=days?`start=${days[0]}&end=${addDays(days.at(-1)!,1)}`:'';
  const quiet=<T,>(p:Promise<T>)=>{p.catch(()=>{});return p;};  // each is awaited (and its failure handled) where it is used
  ahead={id,epoch:scheduleEpoch,range,list:boot&&!workspaceListOffset?quiet(reader<RotaList>('/api/workspaces?limit=50&offset=0')):null,meta:quiet(rotaMeta(id,reader)),
    window:range?quiet(reader<ScheduleWindow>('/api/schedule/sessions?'+range)):null};
  // Not yet "seen" whole: the Planner still draws the window first, while this read runs beside it, quietly (UX-FIX-A,
  // D59: the read behind a drawn week never lights the top bar).
  if(['week','day','month'].includes(schedView))void wholeRota(id,true,false,boot?path=>background<WorkspaceData>(path):undefined).catch(()=>{});
}
/** The reads started ahead for this rota, taken once by the render that uses them. */
function takeAhead(id:string){const a=ahead;if(!a||a.id!==id||a.epoch!==scheduleEpoch)return null;ahead=null;return a;}
/** FE-PERF (scale): a Rules list address starts the page's own reads at boot beside the lists (rule_catalogue.ts
 *  readRulesAhead), not after them. Only where the first draw is that list (allowed, not desktop-only, the rule catalogue
 *  on, no ?source= that may open a rule instead); taken by the next draw, whichever page it is, so never kept for later. */
let rulesAhead:ReturnType<typeof readRulesAhead>|null=null;
const rulesListFirst=()=>view==='rules'&&!routeParts.length&&!routeQuery.has('source')&&ruleCatalogueOn()&&allowed(view)&&(!phoneLite()||fitsPhone(view,routeParts,routeQuery));
/** A rota's days in view: compact detail, then every page of its sessions and findings there. A rota that changes while
 *  the pages are read is read whole instead. */
async function readWindow(meta:Awaited<ReturnType<typeof rotaMeta>>,days:string[]):Promise<WorkspaceData|null>{
  const id=meta.workspace.id,range=`start=${days[0]}&end=${addDays(days.at(-1)!,1)}`;
  // Findings (items) and coverage are paged side by side: read on while either has more, by the page size (a page can
  // hold coverage and no findings).
  const pages=async(part:'shifts'|'validation')=>{const items:Row[]=[],coverage:Row[]=[];
    for(let offset=0;;){const p=await get<WorkspacePartPage>(`/api/workspaces/${encodeURIComponent(id)}/parts?part=${part}&${range}&offset=${offset}`);
      if(p.revision!==meta.workspace.revision)return null;items.push(...p.items as Row[]);coverage.push(...(p.coverage||[]) as Row[]);
      if(!p.has_more&&!p.coverage_has_more||!p.limit)return {items,coverage};offset+=p.limit;}};
  const [shifts,checks]=await Promise.all([pages('shifts'),pages('validation')]);
  if(!shifts||!checks)return null;
  // The window's open places and filled counts (coverOf) come from its coverage rows, as the whole rota's do.
  const validation=meta.workspace.validation?{...meta.workspace.validation,findings:checks.items,coverage:checks.coverage}:meta.workspace.validation;
  return {...(meta as unknown as WorkspaceData),shifts:shifts.items as PlannerSession[],workspace:{...meta.workspace,validation},partial:true};
}
// A draft edit, forward or Undo, keeps what this visit read except the draft itself (see patchDraft): the Schedule
// redraws reading only the draft. `inPlace`: every save since the last refresh was patched so (refreshOnce).
let inPlace=true;
window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail;if(!patchDraft(d?.result,d?.kind||'')){forgetSchedule();inPlace=false;}});
// Session saves use the same must-fix total as the Fix list and footer.
window.addEventListener('rota-saved',e=>{
  const {kind,result}=(e as CustomEvent).detail||{};
  const must=kind==='shift.create'||kind==='shift.update'?fixDeltaOf(result)?.summary?.must:0;
  toastClause(must?mustWords(must)+'.':deltaWords(result));
});
/** A draft edit's reply (every draft edit and its Undo answer alike, planning.changed): the draft's row in the rota list
 *  takes the reply's rota and health summary, the sidebar's open-places count follows, and the draft (and the calendar
 *  windows showing it) are read again: a reply carries only a summary of the check, not its findings and coverage.
 *  Dates that moved (an overlap fix) change the list's overlap flags, which are the server's to say: then everything is
 *  read again (false). */
function patchDraft(r:Row,kind:string):boolean{
  // A job command (run, cancel, add time) changes no draft: the panel reads its jobs, nothing else is read again (G-45).
  if(kind.startsWith('job.')){lastJobs=null;return true;}
  // A read changes no draft: the draft held stays (dropping it read the whole rota again after an edit or undo, and
  // left an issue list opened meanwhile without its Fix list).
  if(READ_ONLY_COMMANDS.has(kind))return true;
  if(kind==='generation.apply'){
    const job=applyWrote(r)?r.job:null,kept=lastJobs;
    if(job)generationApplyEpoch++;
    const jobs:Row[]=kept?.jobs.jobs||[];
    lastJobs=job&&kept&&kept.id===job.workspace_id&&kept.activity===seenActivity&&jobs.some(j=>j.id===job.id)
      ?{...kept,jobs:{...kept.jobs,jobs:jobs.map(j=>j.id===job.id&&j.revision<=job.revision?job:j)}}:null;
  }else if(kind==='department.settings'||kind.startsWith('workspace.baseline.'))lastJobs=null;
  const id:string=r?.workspace?.id||'',row=plannerMemo?.page.workspaces.find(x=>x.id===id),held=scheduleCache.drafts.get(id);
  // SW3-B-03: a submit or an approval answers with the rota's new state (no health): the list takes it too, so the page
  // redraws in that state reading only the rota, not the whole rota list (minutes under load, and a failed read left
  // "Waiting for approval" with Approve still offered after the approval).
  // SW3-B-03b: a return or a reopen too (workspace.reopen); a refused decision's re-read rota as well (rota-conflict).
  const lifecycle=ROTA_DECISIONS.has(kind)&&kind!=='workspace.publish';
  if(lifecycle)forgetChanges(r?.workspace?.base_publication_id);
  // Final swap approval can publish the rota in the same command. Hand off both publication replies before refreshing.
  if((kind==='workspace.publish'||kind==='workspace.approve')&&r?.publication_id){
    if(id&&workspaceId===id&&view==='schedule'&&!schedSubPage()&&location.hash===routeSeen){
      workspaceId='';sourcePublication='';draftNone=true;
      writeRoute();
      document.querySelector('.planner-bar .rota-status')?.replaceWith(rotaStatusPill(r.workspace));
      document.querySelector('.planner-bar .rota-decisions')?.replaceChildren();
    }
    return false;  // The published schedule and rota list both need fresh reads.
  }
  // P0 edit latency (live capture, 7 Nov 2026): a rota past the list's first page has no row in it; its held draft is
  // patched all the same (the dates it is checked against are the draft's), not dropped and read whole after every edit.
  const was=row||held?.workspace;
  if(!r?.health&&!lifecycle||!was||was.starts_on!==r.workspace.starts_on||was.ends_on!==r.workspace.ends_on)return false;
  // A new row, not the old one changed: the draft on screen shares it until the draft is read again.
  // RESP-REVIEW: a reply with its delta (edit, Undo, Redo) patches the draft held, so the redraw reads nothing; any
  // other reply, a stale delta or a revision that does not follow on drops it and the draft is read whole (edit_delta.ts).
  const patched=applyEditDelta(held,r);
  if(patched)scheduleCache.drafts.set(id,patched);else scheduleCache.drafts.delete(id);
  if(row&&plannerMemo){const list=plannerMemo.page.workspaces;list[list.indexOf(row)]={...row,...r.workspace,health:r.health??row.health};}
  for(const path of [...scheduleCache.windows.keys()])if(path.includes(id))scheduleCache.windows.delete(path);
  if(kind.startsWith('job.'))lastJobs=null;
  if(plannerMemo&&!workspaceListOffset&&!plannerMemo.page.has_more){const b=scheduleBadge(plannerMemo.page.workspaces);if(b)navBadges.schedule=b;else delete navBadges.schedule;applyNavBadges();}
  if(kind!=='generation.apply'&&lastJobs?.jobs?.jobs?.some((j:Row)=>j.status==='applied'))lastJobs=null;  // an undone auto-schedule is no longer applied
  return true;
}
// People: work contacts from /api/directory for roles without the catalogues (staff); the open person's side panel.
let directoryRows:Row[]|null=null,personPanel:{id:string;redraw:()=>void}|null=null;
// Rules just proposed: the next Rules page brings them into view (scrolled to, highlighted, focused).
let revealRules:string[]=[],revealAgain=0;
window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail;
  if(['policy.create','policy.group'].includes(d?.kind))revealRules=[d.result?.record,...(d.result?.records||[])].filter(Boolean).map((r:Row)=>r.id);});
/** The Rules page's open saved view (table_view 'rules'), kept for this visit like rulesView. */
let rulesSavedId='';
/** D22: what a group run established about the other groups' plan (P2-BE `completion.status`), said as it is: a plan
 *  shown possible, shown impossible, not established, or not checked. Never "fine" by default. */
function completionLine(job:Row):HTMLElement|string{
  const c=(job.result?.completion||job.completion) as {status?:string;required_group_ids?:string[]}|undefined;if(!c?.status)return '';
  const WORDS:Record<string,[string,IconName,string]>={OPTIMAL:['A plan for the other groups was found','check','ok'],FEASIBLE:['A plan for the other groups was found','check','ok'],
    witness_found:['A plan for the other groups was found','check','ok'],INFEASIBLE:['No plan for the other groups is possible with these placements','alert','err'],
    infeasible:['No plan for the other groups is possible with these placements','alert','err'],UNKNOWN:['Not established whether the other groups can still be planned','clock','warn'],
    unknown:['Not established whether the other groups can still be planned','clock','warn'],NOT_CHECKED:['Room for the other groups was not checked','clock','neutral'],not_checked:['Room for the other groups was not checked','clock','neutral']};
  const [text,glyph,tone]=WORDS[c.status]||[`Other groups: ${c.status}`,'clock' as IconName,'neutral'];
  return el('p',{class:'g-held g-completion '+tone,role:'status'},icon(glyph),text);
}
/** The configured Rota Coordinators (drafts.ts rotaCoordinators), once read. */
let rotaCoordinatorIds:string[]=[];
/** SW3-A-11: why a private draft has no rota to merge into, and who can change that (one sentence, no full stop). */
const mergeBlocked=()=>rotaCoordinatorIds.length?'Merging needs the rota for this period. A Rota Coordinator makes one with Make this the rota'
  :`${NO_ROTA_COORDINATORS}, so this period has no rota to merge into. An admin chooses them in Configuration › Scheduling groups`;
/** The scheduling group shown per rota (maintenance): '' is All groups; unset, the viewer's own group (planning_context.group_id). */
const groupScope=new Map<string,string>();
const scopedGroup=(rota:string,p:PlanningContext|null):GroupState|null=>{if(!p?.groups?.length)return null;
  const id=groupScope.has(rota)?groupScope.get(rota):p.group_id||'';return p.groups.find(g=>g.group_id===id)||null;};
/** The planner bar's group scope (maintenance): the Saved-view button of the collection toolbar, listing the groups and All groups. */
function groupScopeButton(rota:string,p:PlanningContext){
  const g=scopedGroup(rota,p),label=g?.name||'All groups';
  const b=el('button',{type:'button',class:'dt-tool g-scope','aria-haspopup':'dialog','aria-expanded':'false','aria-label':'Scheduling group: '+label},
    g?avatar(label):icon('people'),el('span',{class:'dt-tool-label g-scope-name'},label),icon('chevronDown'));
  b.addEventListener('click',()=>{if(b.getAttribute('aria-expanded')==='true')return;
    openPicker(b,b.parentElement||document.body,{label:'Scheduling group',multiple:false,recent:false,selected:new Set([g?.group_id||'']),
      choices:[...p.groups!.map(x=>({value:x.group_id,label:x.name,hint:[x.is_mine||x.group_id===p.group_id?'Yours':'',x.must?mustWords(x.must):'Ready'].filter(Boolean).join(' · ')})),{value:'',label:'All groups'}],
      // SX-23 (maintenance): All groups runs for the whole department population; a people scope kept from a group run
      // is dropped, so the next run is never quietly the earlier group's.
      onPick:v=>{groupScope.set(rota,v);if(!v){const s={...freshScheduling(rota),...ownSetup(rota),...schedulingByRota.get(rota)};
        Object.assign(s,{group:null,pickGroup:false,scopeError:''});schedulingByRota.set(rota,s);if(schedulingSettings.workspace===rota)schedulingSettings=s;keptSetup(s);}
        renderPage();}});});
  return b;
}
let rulesView={query:'',search_options:undefined as SearchOptions|undefined,status:'',date:'',offset:0,topics:[] as string[],severity:[] as string[],who:null as Conditions|null,collapsed:[] as string[],more:[] as string[],group:'topic',sort:null as SortValue};
const lastIncludedDate=(exclusive:string)=>new Date(Date.parse(exclusive.slice(0,10)+'T12:00:00Z')-86400000).toISOString().slice(0,10);
/** The planner's bulk session selection, with one change point: any add, delete or clear (a box, Clear, Select all, a
 *  range, a bulk action, a draft switch) tells the rules panel once per task, so the panel follows the set (maintenance). */
class TickedSet extends Set<string>{
  #queued=false;#changed:()=>void;
  constructor(changed:()=>void){super();this.#changed=changed;}
  #note(){if(this.#queued)return;this.#queued=true;queueMicrotask(()=>{this.#queued=false;this.#changed();});}
  override add(v:string){if(!this.has(v)){super.add(v);this.#note();}return this;}
  override delete(v:string){const had=super.delete(v);if(had)this.#note();return had;}
  override clear(){if(this.size){super.clear();this.#note();}}
}
let selected:Set<string>=new TickedSet(()=>selectTicked([...selected])),renderSerial=0,refreshing:Promise<void>|null=null,refreshAgain=false,connected=true;
// On a phone (phone scope, maintenance): Home, the published Schedule (your duties; no planning), Requests (ask, decide), the
// Inbox and approvals open for every account that may open them; `phone` below is the one place that decides. Every
// other page (planning, Configuration, Rules…) is for a desktop.
/** Sections that need a permission beyond Configuration itself (the server refuses the rest): GitHub is the Developer's. */
const SECTION_PERMISSION:Record<string,Permission>={integrations:'integrations.github',ai_provider:'ai.providers',notifications:'notification.policy.update',branding:'branding.configure'};
// `live`: what a page shows, by live-change scope (the pulse's `changes`): a change in any other scope reads nothing here.
// `liveIds`: the records of a scope the page follows (none named: every record of it).
const paths:Record<string,{title:string;/** Help that changes what someone does here (N-15: no taglines). */help?:string;roles:string[];phone?:(parts:string[],query:URLSearchParams)=>boolean;
  live:LiveScope[];liveIds?:()=>Partial<Record<LiveScope,string[]>>}>=withoutPrototype({
  // Every account lands on Home, so Home always fits a phone (its phone branch keeps only phone work): never a refusal.
  home:{title:'Home',roles:[],phone:()=>true,live:['workspace','publication','rules','catalogue','requests','swaps','availability','access','inbox']},
  schedule:{title:'Schedule',roles:[CAPABILITY.STAFF,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR],
    live:['workspace','publication','availability','people','rules','catalogue','requests','swaps','settings'],
    // The Planner follows its own rota (and the one beside it); the Fix list and published views follow every rota.
    liveIds:()=>workspaceId&&!schedSubPage()?{
      workspace:[workspaceId,compareId].filter(Boolean),
      publication:[publicationId,sourcePublication].filter(Boolean),
    }:publicationId?{publication:[publicationId,sourcePublication].filter(Boolean)}:{},
    // Planners read the Fix list on a phone (D10); a coordinator also changes a published session from a phone (Replace, Mark absent).
    phone:(parts,q)=>parts[0]==='issues'?q.get('view')!=='checks'&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)
      // D25 (maintenance): an approver decides a rota in review from a phone (?approve=<rota>).
      :q.has('approve')&&acts('submitted')?!schedSubPage(parts)&&(!q.get('edit')||q.get('edit')===q.get('approve'))
      :!!phoneReview&&q.get('edit')===phoneReview&&acts('submitted')?true
      :!schedSubPage(parts)&&(q.get('edit')||'none')==='none'},
  requests:{title:'Requests',roles:[CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR],phone:()=>true,live:['requests','swaps','availability','rules']},  // person facts change as rules
  // A phone opens People as Where is everyone (owner, v2 defaults: the phone agenda) for whoever reads the timeline;
  // its other layouts stay on the desktop. Staff's contact directory (no lists held) opens there too: finding a colleague.
  people:{title:'People',roles:[CAPABILITY.STAFF,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.CREDENTIALER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD],phone:()=>true,  // maintenance: every role gets the staff contact directory on a phone
    live:['people','availability','publication','catalogue','access']},
  programmes:{title:'Programmes & rotations',help:'External rotations block overlapping duties. Internal rotations allow duties when the person is clinically eligible.',roles:[CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.PROGRAMME_LEAD],live:['people','catalogue','availability','rules']},
  insights:{title:'Insights',roles:[CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.ADMIN,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD],live:['publication','workspace','people','rules','settings','availability','requests']},
  rules:{title:'Rules',roles:[CAPABILITY.RULES_PAGE],live:['rules','catalogue','people','workspace']},
  // Admins and coordinators, plus whoever holds a section's own permission (an operator's GitHub/AI provider): AO-001.
  configuration:{title:'Configuration',roles:[CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,...Object.values(SECTION_PERMISSION)],live:['catalogue','settings','people','rules','access']},
  // Review routes (deciding proposals) on a phone; People & roles and Role history stay on the desktop (PAGE_TABS `phone`).
  access:{title:'Access & approvals',phone:parts=>tabsFor('access').some(t=>t.phone&&(!parts[0]||t.key===parts[0])),roles:[CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR,CAPABILITY.ACCESS_SYSTEM_ROLES,CAPABILITY.ACCESS_INSTALLATION_ADMIN],live:['access','people','rules','requests']},
  system:{title:'System',roles:[CAPABILITY.CATALOGUE_READ,CAPABILITY.SYSTEM_OPERATIONS,CAPABILITY.AUDIT_READ,CAPABILITY.FEEDBACK_READ_ALL],
    live:['jobs','settings','access','people','catalogue','feedback']},
  // The Inbox follows the unread count the pulse carries (onPulse) and changes named for it.
  inbox:{title:TERMS.inbox,roles:[],phone:()=>true,live:['inbox']},
  // Everyone's own reports (feedback.ts); Developers triage all of them in System › Feedback.
  feedback:{title:'My feedback',roles:[],phone:()=>true,live:['feedback']},
});
const permits=(...keys:string[])=>Boolean(me&&keys.some(key=>hasAccess(me!.roles,key)));
const may=(p:Permission)=>Boolean(me&&grants(me.roles,p));
const acts=(status:string)=>Boolean(me&&actsOn(status,me.roles));
const canLookupRules=()=>permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD);
/** Where setup steps lead. Admins change configuration; a coordinator sees the same steps and does the ones they may (announcements, people, rules, rotas). */
const setupOpeners=()=>({can:(kind:string)=>permits(CAPABILITY.ADMIN)||catalogueSection(kind),
  catalogue:(kind:string)=>{if(permits(CAPABILITY.ADMIN))navigate('configuration/'+kind);else void ensureCatalogues().then(()=>catalogueForm(ctx(),kind),()=>{});},page:navigate});
// Not every key is a list (`_versions` is the catalogues' revisions): a name looked up across all of them skips it.
// People come back as person rows (types.ts PersonRow), so the compiler sends their names through person_label.ts.
function rows(kind:'people'):PersonRow[];
function rows(kind:string):Row[];
function rows(kind:string):Row[]{const r=catalogues[kind];return Array.isArray(r)?r:[];}
/** People tags the Schedule can show or colour by (Configuration › Tags records, archived included so a saved choice stays
 *  readable); staff hold no lists, so none. */
const personTagChoices=()=>rows('tags').filter(t=>((t.labels||[]) as string[]).includes('people'));
// Each list's records by id, indexed once per list: keyed by the array itself, so a reloaded list (always a new array)
// is indexed afresh. Every name on every page goes through here (name(), findings' wording), so not a scan per name.
const byIds=new WeakMap<Row[],Map<unknown,Row>>();
function find(kind:'people',id:string):PersonRow|undefined;
function find(kind:string,id:string):Row|undefined;
function find(kind:string,id:string):Row|undefined{const list=rows(kind);let m=byIds.get(list);
  if(!m){m=new Map();for(const r of list)if(!m.has(r.id))m.set(r.id,r);byIds.set(list,m);}return m.get(id);}
// A rule line may name a tag where it names records: it reads "#Tag".
const name=(kind:string,id:string)=>{const r=find(kind,id);return r?labelOf(r):tagLabel(ctx(),id)||labelOf(r);};
setFindingNamer(name);
const personSource=(id:string,label=name('people',id)):Child=>personLink(id,label,navigate);
/** DL-029: People reads the contact directory (/api/directory) for an account without the lists and, on a phone, for
 *  every account that is not a timeline reader: a phone shows Where is everyone or the directory, never the collection
 *  (operator screens stay on the desktop; maintenance gives every role the directory). */
const peopleDirectory=()=>!holdsLists(catalogues)||phoneLite('people')&&!may('schedule.read_team')&&!permits(CAPABILITY.STAFF);
// Every person name (personLink) opens the one hub. DL-027: at its own address (#people?open=<id>), so a reload, a
// shared link and Back bring it back, and Back returns to the list as it was (table_view keeps its filter and scroll).
// The same address on a phone (People is on every phone, maintenance), where Back closes the person's card. A reader People
// cannot show them to (no People page, or not in its projection) gets the hub in place, from the name it was shown with.
onPersonOpen((id,label)=>{const row=(holdsLists(catalogues)?rows('people'):directoryRows||[]).find(p=>p.id===id);
  if(row&&allowed('people')){navigate('people?'+new URLSearchParams({open:id}));return;}
  personDetails(row||{id,display_name:label} as Row);});
/** Configuration › Tags with this tag's editor open (from a tag chip anywhere). */
const openTag=(tag:Row)=>tagEditor(ctx(),tag,permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR));
const zone=()=>me!.organisation.timezone;
// `me` and `catalogues` are read live: a context made at start-up (the Sheet's) or before the deferred lists arrived
// sees the signed-in person and the lists once loaded.
const ctx=():Context=>({get me(){return me!;},get catalogues(){return catalogues;},ready:ensureCatalogues,refresh,navigate,setWorkspace:(id)=>{workspaceId=id;sourcePublication='';draftNone=false;jumpToDraft=true;selected.clear();},permits:(...keys)=>Boolean(me&&keys.some(key=>hasAccess(me!.roles,key))),setup:setupOpeners(),
  fill:(id,s,person)=>openFill(id,s,undefined,person)});
/** A draft session's fill panel on the Schedule (for an issue, or with its search set to one person's name). */
function openFill(id:string,s:Row,issue?:FillIssue,q?:string){workspaceId=id;draftNone=false;queueFill(id,String(s.id),undefined,issue,q);toCalendar(s);navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));}
// People are listed by name; other records keep the order the server gives them (ranks and leave types have their own).
const choices=(items:Row[],people=false)=>{const list=items.map(r=>({value:r.id,label:labelOf(r)}));return people?list.sort(byLabel):list;};
// What the bulk screens read from the app (bulk.ts): condition fields, a field's choices, record names, roles.
// The catalogue a field picks from is its contract `ref`; a field named <thing>_id picks from <things> when it has none.
const refOf=(key:string,ref?:string)=>ref||[key.replace(/_ids?$/,'s'),key.replace(/y_ids?$/,'ies'),key.replace(/_ids?$/,'')].find(k=>catalogues[k]);
setBulkContext({
  conditionFields:keys=>[...ruleConditionFields(ctx()).filter(f=>keys.includes(f.key)&&!isTyped(f.key)),...typedFields(keys)],
  choices:(f,key,collection)=>{if(key==='weekdays')return weekdayOptions();if(key==='tags')return [...new Set(rows(collection).flatMap(r=>(r.tags||[]) as string[]))].sort().map(t=>({value:t,label:t}));
    const kind=refOf(key,f.ref);return kind?choices(rows(kind).filter(r=>r.active!==false),kind==='people'):undefined;},
  permits:(...roles)=>permits(...roles),
  stagedCount:()=>Number((me as Row|null)?.bulk_staged_count||0),
  reviewCount:()=>Number((me as Row|null)?.bulk_review_count||0),identity:()=>me?.identity_id||'',
  labelOf:(collection,id)=>{for(const k of collection&&catalogues[collection]?[collection]:Object.keys(catalogues)){const r=rows(k).find(x=>x.id===id);if(r)return labelOf(r);}return 'Record';},
  open:(collection,id)=>{const r=collection==='people'?rows('people').find(p=>p.id===id):undefined;if(r)personDetails(r);},
});
// The Bulk editor reads the same context (its record types, choices and roles).
queueMicrotask(()=>setSheetContext(ctx()));
const status=(state:string)=>statusBadge(state);

/** Why the sign-in form is shown again, by the server's refusal code: said on the form itself, which survives the reset below. */
const SIGNED_OUT:Record<string,string>={
  SESSION_EXPIRED:'You were signed out after a period of inactivity, or your session was ended. Sign in to continue where you left off.',
  ACCESS_REVOKED:'Your access was revoked. Contact a department admin if this is unexpected.',
  CSRF_FAILED:'Your sign-in needs renewing. Sign in again to save; what you entered stays on the page.',
};
/** Why institutional sign-in came back without signing in (/auth/callback's ?signin_error=…): said once, then taken off
 *  the address so a reload does not repeat it. */
const SIGNIN_ERRORS:Record<string,string>={
  provider_denied:'Your organisation’s sign-in was cancelled.',
  expired:'Sign-in took too long. Try again.',
  config:'Institutional sign-in isn’t set up correctly. Contact an administrator.',
  failed:'Couldn’t sign you in. Try again.',
};
function takeSigninError(){
  const url=new URL(location.href),code=url.searchParams.get('signin_error');if(code===null)return '';
  url.searchParams.delete('signin_error');history.replaceState(history.state,'',url.pathname+url.search+url.hash);
  return SIGNIN_ERRORS[code]||SIGNIN_ERRORS.failed;
}
/** Institutional sign-in that needs a department comes back with ?signin_choice=<continuation>: taken off the address
 *  as the app loads and held only here (never stored or logged) until it is redeemed, cancelled or refused. */
let signinChoice=(()=>{const url=new URL(location.href),value=url.searchParams.get('signin_choice');if(value===null)return '';
  url.searchParams.delete('signin_choice');history.replaceState(history.state,'',url.pathname+url.search+url.hash);return value;})();
/** The department step of institutional sign-in: the server's allowed departments only; Continue redeems the choice (the
 *  server checks the account's current access then sets the session), Cancel ends it. A refusal (expired, changed
 *  settings, re-authentication, no access) is said with "Sign in again", which starts a fresh institutional sign-in. */
async function chooseDepartment(continuation:string,restart:string){
  const error=el('div',{id:'login-error','aria-live':'assertive'}),actions=el('div',{class:'actions'});
  const wrap=el('div',{class:'login-form'},brandRow(),el('h1',{},'Choose a department'),error,actions);
  root.replaceChildren(el('main',{id:'main',class:'login-layout'},el('section',{class:'login-form-wrap'},wrap)));
  const end=()=>oidcCancel(continuation).then(()=>undefined,()=>undefined);
  const cancel=button('Cancel',async()=>{await end();await showLogin();},'secondary');
  const again=button('Sign in again',async()=>{await end();location.assign(restart);},'primary');
  const refused=(err:unknown)=>{error.replaceChildren(note((err as Error).message,'error',err));actions.replaceChildren(again,cancel);again.focus();};
  actions.append(cancel);
  const choices=await oidcDepartments(continuation).catch((err:unknown)=>{refused(err);return null;});if(!choices)return;
  if(!choices.departments.length){refused(new Error('No department is available for this account.'));return;}
  const department=select('Department',choices.departments.map(d=>({value:d.id,label:d.name})));
  const submit=el('button',{type:'submit',class:'button primary'},'Continue');
  const form=el('form',{},department.wrap,submit);
  form.addEventListener('submit',async e=>{e.preventDefault();if(submit.getAttribute('aria-busy')||submit.disabled)return;
    error.replaceChildren();const done=busy(submit,'Signing in…');
    try{const session=await oidcChoose(continuation,department.input.value);
      // Back where sign-in started, on this site only; then the app starts as after any sign-in.
      const to=new URL(session.return_to,location.origin);history.replaceState(null,'',to.origin===location.origin?to.pathname+to.search+to.hash:'/');
      rotatedNow();await done();await boot(root.querySelector<HTMLElement>('.login-layout'));}
    catch(err){await done();form.remove();refused(err);}});
  wrap.insertBefore(form,actions);department.input.focus();
}
/** D15 (B7 signin): a sign-in from another address than the server's own is refused (ORIGIN_REJECTED). With the configured
 *  address in the refusal (`canonical_origin`) the page goes there; until it has, or without one, it says in plain words
 *  where to sign in, and the form that cannot work here goes. */
function wrongOrigin(err:unknown,card:HTMLElement){
  if(!(err instanceof ApiError)||err.code!=='ORIGIN_REJECTED')return false;
  let to:URL|null=null;
  try{const raw=String((err.details as {canonical_origin?:unknown}|undefined)?.canonical_origin||'');to=raw?new URL(raw):null;}catch{to=null;}
  if(to&&(!/^https?:$/.test(to.protocol)||to.origin===location.origin))to=null;
  const href=to?to.origin+location.pathname+location.hash:'';
  let after=false;for(const n of [...card.children]){if(after)n.remove();else if(n.tagName==='H1')after=true;}
  card.append(el('div',{class:'notice warning',role:'status'},el('strong',{},`Sign in at ${brand().short_name}’s usual address.`),` This address (${location.host}) can’t keep you signed in.`));
  if(to){const go=el('a',{class:'button primary login-go',href},`Go to ${to.host}`);card.append(go);go.focus();location.replace(href);}
  return true;
}
/** Everything this page holds for the signed-in account, forgotten (a sign-out, or a session ended for another account):
 *  the page's state, every cache (api.ts clearSessionState), the live stream and the periodic check (both need `me`),
 *  and what was drawn beside the app (dialogs, messages, a result card): nothing of the account stays on the page. */
function forgetSession(){setTableAccount();watchPanelJob('');leaveCheck=null;
  trackStatusFooter(null);
  navBadges={};badgesLoaded=0;badgesRun++;badgesLoading=false;badgesOwed=null;lastSaved=0;forgetNames();
  inboxSnapshot='';inboxPulseKey='';inboxPending=null;publishedCache=null;
  deferredPulse=null;burst=null;seenActivity=null;seenUnread=null;streamSeen=0;streamFailures=0;streamResume=0;lastPoll=0;booting=false;
  document.querySelector('#toast-tray')?.replaceChildren();
  directoryRows=null;personPanel=null;rotaTotal=null;configPicked=false;
  retiredRules=null;effectiveSources={};retiredVisit++;
  forgetSchedule();wholeSeen.clear();lastJobs=null;proposal=null;importTab='staff';schedulingSettings=freshScheduling();schedulingByRota.clear();
  rulesView={query:'',search_options:undefined,status:'',date:'',offset:0,topics:[],severity:[],who:null,collapsed:[],more:[],group:'topic',sort:null};rulesSavedId='';resetFairness();
  retiredRules=null;retiredVisit++;ruleDetailView.clear();rulePanelIds.clear();
  renderSerial++;stream?.close();stream=null;me=null;catalogues={people:[]};hydrating=null;workspaceId='';publicationId='';searchText='';schedSearch=undefined;schedFilters={};schedConditions=null;workspaceListOffset=0;operationsOffset=0;planMode='week';planRange='week';plannerRota=null;schedView='week';viewRange='';viewNames=null;viewDays={...DAYS_DEFAULT};gridColumns='day';gridCells='cards';gridRows='person';gridSubgroup='none';peopleCells='codes';planDay='';pickedDay='';layerMode=LAYER_DEFAULTS.mode;compareId='';mineOnly=LAYER_DEFAULTS.mine;draftNone=false;draftsShown=LAYER_DEFAULTS.draft;sourcePublication='';phoneReview='';publishedLive=null;selected.clear();clearSessionState();
  document.querySelectorAll('dialog,body>.asl-result').forEach(d=>d.remove());
  document.querySelectorAll('body>[aria-live]').forEach(n=>n.replaceChildren());}
/** Signed out mid-work (maintenance): `resume` is the account that was signed in. The page stays as it is, hidden under a
 *  full-screen sign-in; the same account signing in again finds every open form and typed entry where it left them.
 *  Another account reloads the app, so nothing of the first one is shown to it. */
const resuming=()=>!!document.querySelector('dialog.login-resume');
async function showLogin(reason='',resume=''){
  reason=takeSigninError()||reason;
  if(resume&&resuming())return;
  setTableAccount();  // no table search or filter is held while signed out (it may hold private words)
  if(!resume){forgetSession();documentTitle('Sign in');}
  let config:{password_login?:boolean;development_login?:boolean;institutional_login:boolean;environment:string;deployment:string};
  try {config=await get('/auth/config');} catch(e) {bootFailure(e,()=>void showLogin(reason));return;}
  // Sign-in on this page with a password where the server checks them (older servers call it development_login).
  const passwords=config.password_login??!!config.development_login;
  // The account last used on this device: "Continue as <name>", with its password only (or the institutional
  // sign-in), "Use another account", and a remove button for shared computers.
  const known=rememberedAccount(),continuing=known&&(passwords?!!known.username:config.institutional_login)?known:null;
  // The brand (the app's own mark) and "Sign in": an internal tool needs no story (N-06); the account card says who.
  // Test or production as the footer shows it: an icon in the card's corner, named in its tooltip.
  const production=config.deployment==='production';
  const formWrap=el('div',{class:'login-form'},envBadge(production,production?'Production: real rotas and people':'Test environment: test accounts only'),brandRow(),el('h1',{},'Sign in'),reason?el('div',{class:'notice warning',role:'status'},reason):null);
  // Institutional sign-in returns to this page, as the built-in one does; Remember me goes with it.
  const back:Record<string,string>=location.hash.length>1?{next:location.hash}:{};
  const startUrl=(remembered:boolean)=>'/auth/start?'+new URLSearchParams({...back,...(remembered?{remember_me:'true'}:{})});
  if(signinChoice&&!resume&&config.institutional_login){const continuation=signinChoice;signinChoice='';await chooseDepartment(continuation,startUrl(false));return;}
  const remember=check('Remember me');
  // Institutional sign-in stays reachable whenever it is on: the whole form without passwords, else one compact link
  // under the identifier form (an identity linked to the provider may have no password account).
  const institutional=config.institutional_login?(passwords?el('a',{class:'button',href:startUrl(false)},'Continue with institution')
    :el('a',{class:'button primary',href:startUrl(false)},continuing?'Continue as '+continuing.name:'Continue with institutional sign-in')):null;
  institutional?.addEventListener('click',()=>{institutional.setAttribute('href',startUrl(remember.input.checked));});
  const chip=continuing?el('div',{class:'login-account'},avatar(continuing.name),
    el('div',{class:'who',id:'login-account-who'},el('strong',{class:'truncate'},continuing.name),continuing.username?el('small',{class:'truncate'},continuing.username):null),
    iconButton('Remove '+continuing.name+' from this device','close',()=>{forgetAccount();void showLogin(reason);})):null;
  if(chip)formWrap.append(chip);
  let password:ReturnType<typeof field>|null=null,forget:(()=>void)|null=null;
  if(passwords){
    // Always identifier first: the server decides from the username or email (the password step, or the institutional
    // provider); an unknown one behaves like any password account.
    const username=field('Username or email','text',continuing?.username||'',{required:true,autocomplete:'username',spellcheck:false,autocorrect:'off',autocapitalize:'none'});password=field('Password','password','',{id:'current-password',required:true,autocomplete:'current-password'});
    password.wrap.querySelector('label')!.htmlFor=password.input.id;
    if(continuing)password.input.setAttribute('aria-describedby','login-account-who');  // whose password, said with the field
    // The password step keeps the username in the form, hidden (password managers read it); continuing, the account
    // chip says who, else the name typed with "Not you? Change".
    const pw=password,submit=el('button',{type:'submit',class:'button primary'},'Continue');
    // Another account starts with Continue available: its own countdown, if any, comes from the server (the same as forget).
    const who=el('strong',{class:'truncate'}),whoRow=el('div',{class:'login-who'},who,el('button',{type:'button',class:'link-button',onclick:()=>{
      stopWait();pw.input.value='';forgetDepartment();error.replaceChildren();pw.input.removeAttribute('aria-invalid');showPassword(false);username.input.focus();}},'Not you? Change'));
    // onChip: the remembered account's chip says who; "Use another account" turns it off for good.
    let identified=false,onChip=!!continuing;
    const showPassword=(on:boolean)=>{identified=on;pw.wrap.hidden=!on;pw.input.required=on;username.wrap.hidden=on||onChip;
      whoRow.hidden=!on||onChip;who.textContent=username.input.value.trim();submit.textContent=on?'Sign in':'Continue';};
    const form=el('form',{}),error=el('div',{id:'login-error','aria-live':'assertive'});form.append(whoRow,username.wrap,password.wrap,error,remember.wrap,submit);
    let department:Field<HTMLSelectElement>|null=null;
    const forgetDepartment=()=>{department?.wrap.remove();department=null;};
    showPassword(false);
    username.input.addEventListener('input',forgetDepartment);pw.input.addEventListener('input',forgetDepartment);
    // Too many attempts: a calm countdown with the button unavailable until it ends; what was typed and Remember me stay.
    let waitTimer=0;
    const stopWait=()=>{window.clearInterval(waitTimer);submit.disabled=false;};
    const wait=(seconds:number)=>{window.clearInterval(waitTimer);let left=Math.max(1,Math.ceil(seconds));
      const count=el('span',{'aria-live':'off'},String(left)),message=note('Too many attempts. Try again in ','info');message.append(count,' s.');
      submit.disabled=true;error.replaceChildren(message);
      waitTimer=window.setInterval(()=>{left--;if(!form.isConnected||left<=0){window.clearInterval(waitTimer);submit.disabled=false;error.replaceChildren();return;}count.textContent=String(left);},1000);};
    const delay=(err:unknown)=>err instanceof ApiError&&err.status===429?Number((err.details as {retry_after?:unknown}|undefined)?.retry_after)||1:0;
    // A wrong password for the remembered account names that account, with the way to another one beside it.
    const refused=(err:unknown)=>{if(wrongOrigin(err,formWrap))return;const s=delay(err);if(s){wait(s);return;}
      const named=identified&&onChip&&continuing&&err instanceof ApiError&&err.code==='INVALID_LOGIN'?continuing:null;
      const message=note(named?`Unable to sign in as ${named.name} with this password.`:(err as Error).message,'error',err);
      if(named)message.append(' ',el('button',{type:'button',class:'link-button',onclick:useAnother},'Use another account'));
      error.replaceChildren(message);};
    // Only the button changes while signing in (spinner, "Signing in…", fields locked). A refusal keeps what was
    // typed and shows its reason; success opens the app frame at once (boot).
    form.addEventListener('submit',async e=>{e.preventDefault();if(submit.getAttribute('aria-busy')||submit.disabled)return;
      const inputs=[username.input,pw.input];inputs.forEach(i=>{i.readOnly=true;});error.replaceChildren();
      pw.input.removeAttribute('aria-invalid');
      if(onChip)pw.input.setAttribute('aria-describedby','login-account-who');else pw.input.removeAttribute('aria-describedby');
      const accountActions=[...formWrap.querySelectorAll<HTMLButtonElement>('.login-account button,.login-switch')];accountActions.forEach(b=>{b.disabled=true;});
      if(department)department.input.disabled=true;
      const done=busy(submit,identified?'Signing in…':'Continuing…');
      const release=async()=>{await done();inputs.forEach(i=>{i.readOnly=false;});accountActions.forEach(b=>{b.disabled=false;});if(department)department.input.disabled=false;};
      if(!identified){
        try{const {method}=await identify(username.input.value.trim());
          if(method==='institutional'){signingInAs(username.input.value);location.assign(startUrl(remember.input.checked));return;}
          await release();showPassword(true);pw.input.focus();}
        catch(err){await release();refused(err);username.input.focus();}
        return;}
      try{signingInAs(username.input.value);await login(username.input.value,pw.input.value,department?.input.value,remember.input.checked);if(resume){await resumeAs(resume);return;}}
      catch(err){await release();
        const departments=err instanceof ApiError&&err.code==='DEPARTMENT_REQUIRED'?(err.details as {departments?:{id:string;name:string}[]}|undefined)?.departments:null;
        if(Array.isArray(departments)&&departments.length&&departments.every(d=>d&&typeof d.id==='string'&&typeof d.name==='string')){
          forgetDepartment();department=select('Department',departments.map(d=>({value:d.id,label:d.name})));
          form.insertBefore(department.wrap,remember.wrap);announce('Choose a department to continue.');department.input.focus();return;
        }
        refused(err);
        if(err instanceof ApiError&&err.code==='FORBIDDEN'){department?.input.focus();return;}
        if(err instanceof ApiError&&err.code==='UNPROVISIONED_IDENTITY')return;
        pw.input.setAttribute('aria-invalid','true');pw.input.setAttribute('aria-describedby',onChip?'login-account-who login-error':'login-error');pw.input.focus();pw.input.select();return;}
      rotatedNow();
      await boot(root.querySelector<HTMLElement>('.login-layout'));});formWrap.append(form);
    // A local countdown belongs to the name it was for: another account starts with Continue available (the server still decides).
    if(continuing)forget=()=>{stopWait();onChip=false;forgetDepartment();username.input.value='';pw.input.value='';remember.input.checked=false;
      error.replaceChildren();pw.input.removeAttribute('aria-invalid');pw.input.removeAttribute('aria-describedby');showPassword(false);username.input.focus();};
  }
  // "Use another account": back to the generic sign-in (the empty form, or the plain institutional button).
  function useAnother(){chip?.remove();another?.remove();
    if(institutional&&!passwords)institutional.textContent='Continue with institutional sign-in';
    if(forget)forget();else institutional?.focus();}
  const another=continuing?el('button',{type:'button',class:'link-button login-switch',onclick:useAnother},'Use another account'):null;
  if(institutional)formWrap.append(...(passwords?[institutional]:[remember.wrap,institutional]));
  if(another)formWrap.append(another);
  if(!config.institutional_login&&!passwords)formWrap.append(note('Identity integration is not configured. Contact the institutional operator.','error'));

  // shortcut: institutional sign-in leaves the page (its form is lost); keep it too with a pop-up sign-in if that provider is used.
  if(resume){if(resuming())return;  // several reads refused at once: one sign-in (another call drew it during the config read)
    const shade=el('dialog',{class:'login-resume','aria-label':'Sign in'},el('section',{class:'login-form-wrap'},formWrap));
    shade.addEventListener('cancel',e=>e.preventDefault());document.body.append(shade);shade.showModal();}
  else root.replaceChildren(el('main',{id:'main',class:'login-layout'},el('section',{class:'login-form-wrap'},formWrap)));
  // Continuing: Continue (or the institutional button) has focus; else the username (N-07).
  if(continuing)formWrap.querySelector<HTMLElement>('form button[type=submit],a.button')?.focus();else formWrap.querySelector<HTMLInputElement>('input:not([type=hidden])')?.focus();
}

/** The same account (and department) is back: the sign-in lifts off the kept page (Save again sends what was typed); another account
 *  starts the app afresh. */
async function resumeAs(who:string){
  const account=await request<Me>('/api/me');
  if(accountKey(account)!==who){leaveCheck=null;location.reload();return;}
  const shade=document.querySelector<HTMLDialogElement>('dialog.login-resume');
  // Nothing typed to keep: the page is drawn afresh (a read that failed on the way out is asked again).
  if(!document.querySelector('dialog[open]:not(.login-resume)')&&!dockDirty()&&!leaveCheck?.()){await boot();if(shade){shade.close();releaseToasts(shade);shade.remove();}return;}
  me=account;applyDisplay();if(shade){shade.close();releaseToasts(shade);shade.remove();}listen();  // close() returns focus to the kept field
}
/** One account in one department: what a kept page belongs to. */
const accountKey=(m:Me)=>m.identity_id+'.'+m.department.id;
/** A directory section in words: an imported code ("family-medicine") reads "Family medicine". */
const sectionLabel=(p:Row)=>{const s=String(p.directory_section||'');return /^[a-z0-9]+(-[a-z0-9]+)+$/.test(s)?s.replaceAll('-',' ').replace(/^./,c=>c.toUpperCase()):s;};
// The Names default (department, or the person's own choice) as a label; a view's Person label replaces it.
// The names default (department, or the person's own choice) as the contract maps it to a label (NAME_LABEL_DEFAULTS).
const NAME_LABEL=NAME_LABEL_DEFAULTS as unknown as Record<NameMode,PersonLabel>;
/** The department's display settings; with person labels on (an admin flag), its own Person label is the default (D75). */
const displaySettings=()=>((me?.department as Row|undefined)?.display_settings||{}) as Row;
const labelsDesigned=()=>displaySettings().person_labels_enabled===true;
const departmentLabel=()=>labelsDesigned()&&displaySettings().person_label?displaySettings().person_label as PersonLabel:null;
/** The label a screen without its own view uses: the person's own choice, else the department's. */
const defaultLabel=():PersonLabel=>!ownNameMode()&&departmentLabel()||NAME_LABEL[nameMode()];
// Month's day columns are narrow: there the department's Full-name default gives way to short names (a view's label or
// the person's own Names choice still wins).
// Month and a phone default to short names (FIXLIST-UX review); a name mode chosen in View settings wins.
// Paper is not the phone's width: a page drawn for print or export from a phone keeps the names default (maintenance).
const labelNow=():PersonLabel=>viewNames||(!ownNameMode()&&!departmentLabel()&&nameMode()==='full'&&(schedView==='month'||phoneQuery.matches&&!weekView.forPrint)?NAME_LABEL.short:defaultLabel());
useLabel(labelNow);
/** A person by id for every name on every screen (one map per catalogue load, so naming a long list stays linear). */
let personIndex:{rows:Row[];by:Map<string,Row>}|null=null;
const personById=(id:string)=>{const list=rows('people');if(personIndex?.rows!==list)personIndex={rows:list,by:new Map(list.map(p=>[p.id,p]))};return personIndex.by.get(id);};
useNames({rows,person:personById,label:defaultLabel});
/** Sidebar and command-menu label: the page title up to ' & ' (Workload & reports → Workload), so both always start with the same word. */
/** Sidebar and command-menu label: the page title up to ' & ' (Programmes & rotations → Programmes), so both always start with the same word. */
const navLabel=(id:string)=>id==='access'?paths[id].title:paths[id].title.split(' & ')[0];
const navIcons:Record<string,IconName>={home:'grid',schedule:'calendar',requests:'leave',people:'people',programmes:'book',insights:'chart',configuration:'settings',rules:'rules',inbox:'inbox',access:'shield',system:'monitor'};  // System is not Configuration: its own icon (G-04)
// Ten pages. Merged pages keep working through ROUTE_ALIASES (routes.ts).
const navGroups:[string,string[]][]=[['Work',['home','schedule','requests']],['Team',['people','programmes','insights']],['Setup',['rules','configuration','access','system']]];
// The inbox is personal, not admin: the bell in the top bar opens it (with its unread count), so it has no sidebar entry.
/** A page made of tabs (PAGE_TABS) is open to anyone who may see one of its tabs. */
const tabsFor=(id:string)=>(PAGE_TABS[id]||[]).filter(t=>permits(...t.roles));
/** The tabs this visit offers: on a phone (phone scope) only the page's phone tabs, so no tab leads into the desktop notice. */
const shownTabs=(id:string)=>{const all=tabsFor(id),phone=phoneLite(id)?all.filter(t=>t.phone):[];return phone.length?phone:all;};
const allowed=(id:string)=>!!paths[id]&&(PAGE_TABS[id]?tabsFor(id).length>0:!paths[id].roles.length||permits(...paths[id].roles)||id==='programmes'&&!!me?.trainee);
/** In the sidebar: staff-only accounts keep four pages (Insights stays one link away on Home); a coordinator sees
 * Configuration only while the setup guide still has steps (it is the guide's home). */
const inNav=(id:string)=>allowed(id)&&phoneNav(id)&&!(id==='insights'&&me!.roles.every(r=>hasAccess([r],CAPABILITY.STAFF)));
/** How many rotas exist, once read (setup guide, sidebar badges): the setup step "Create your first rota" counts only for
 * people who can see rotas, so the guide, the sidebar badge and the sidebar entry agree. */
let rotaTotal:number|null=null,configPicked=false;
const rotaCount=()=>permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)?rotaTotal:null;
const requiredSteps=(counts?:Record<string,number>)=>setupSteps(ctx(),setupOpeners(),rotaCount(),counts).filter(x=>!x.optional);
function setupComplete(){return requiredSteps().every(stepDone);}
function accountMenu(anchor:HTMLElement){
  popover(anchor,(panel,close)=>{
    // Escape closes the menu and folds the rail it opened from (LIVE-49); focus stays on the account button (APG menu button).
    const fold=(e:KeyboardEvent)=>{if(!panel.isConnected||e.key!=='Escape'){if(!panel.isConnected)document.removeEventListener('keydown',fold,true);return;}
      document.removeEventListener('keydown',fold,true);
      setTimeout(()=>{const s=frame?.sidebar;if(!s?.contains(anchor)||!s.classList.contains('peek')||s.classList.contains('navigation-open'))return;
        s.classList.remove('peek');});};
    document.addEventListener('keydown',fold,true);
    // Appearance is three menuitemradio items in the one menu (N-16): arrows move focus, Enter or Space chooses.
    const choice=(value:Theme,label:string,name:IconName)=>{const b=el('button',{type:'button',role:'menuitemradio','aria-checked':String(theme()===value)},icon(name),label);b.addEventListener('click',()=>{setTheme(value);panel.querySelectorAll('[role=menuitemradio]').forEach(n=>n.setAttribute('aria-checked',String(n===b)));});return b;};
    panel.append(
      el('div',{class:'popover-head'},avatar(me!.display_name),el('div',{class:'truncate'},el('strong',{},me!.display_name),el('small',{class:'account-roles'},me!.roles.map(roleLabel).join(' · ')))),
      // Only what belongs to the account: the bell opens the inbox, the sidebar and Ctrl K search, Setup has its own page.
      el('div',{class:'menu-list',role:'menu','aria-label':'Account'},
        el('div',{class:'popover-section'},el('span',{class:'popover-label',id:'theme-label','aria-hidden':'true'},'Appearance'),el('div',{class:'segmented',role:'group','aria-labelledby':'theme-label'},choice('light','Light','sun'),choice('dark','Dark','moon'),choice('system','System','monitor'))),
        // Motion is the viewer's own choice (REQ-MOTION): Still shows auto-schedule changes without animation; the
        // department's Off and the device's reduced motion apply whatever this says.
        permits(CAPABILITY.COORDINATOR)&&animationSettings(me!.department as Row).mode!=='off'?(()=>{const b=menuItem('Animate auto-schedule','sparkle',()=>{setMotionStill(!motionStill());b.setAttribute('aria-checked',String(!motionStill()));},{role:'menuitemcheckbox','aria-checked':String(!motionStill())});
          b.append(el('span',{class:'switch-mark','aria-hidden':'true'}));return b;})():null,
        // Alerts, devices and lock-screen detail (P4); Install app only while this browser can install it (P1, D31).
        menuItem('Notifications','bell',()=>{close();notificationsPanel(me!.department.id);}),
        installWay()?menuItem('Install app','download',()=>{close();void install();}):null,
        // A phone has no keyboard shortcuts to show.
        phoneQuery.matches?null:menuItem('Keyboard shortcuts','keyboard',()=>{close();shortcutsDialog();},{},'?'),
        // Feedback: captured before the menu's own page state changes (the menu closes first).
        menuItem('Send feedback','message',()=>{close();openFeedback();},{},phoneQuery.matches?'':FEEDBACK_KEY),
        menuItem('My feedback','inbox',()=>{close();navigate('feedback');}),
        menuItem(`About ${brand().short_name}`,'help',()=>{close();aboutDialog();}),
        me!.account?menuItem('Change password','lock',()=>{close();changePasswordDialog(me!.account!.username);}):null,
        el('hr',{class:'menu-sep'}),
        menuItem('Sign out','logout',()=>{close();void signOut();},{class:'danger'})));
  },{label:'Account',placement:anchor.classList.contains('mobile-account')?'below-end':'above-start'});
}
// maintenance, the one sign-out (Account menu, Ctrl K, the start-up failure screen): the page and everything kept for the
// account go at once, and the sign-in form shows only once the server says the session ended (its cookie is httpOnly:
// only the server can end it). A refusal, or no answer within SIGN_OUT_TIMEOUT_MS, says so with Try again; nothing of
// the account comes back. The address goes too (LIVE-50): it holds the Schedule's day, draft and filters, which the
// next account must not inherit. Other open tabs of the app blank and sign out with it (channel fmuc-session).
const SIGNED_OUT_AT='fmuc.signed-out-at',pageLoadedAt=Date.now();let ownSignOutAt='';
/** Another tab of this device signed out after this page loaded (this page's own sign-out is followed by its own sign-in). */
const signedOutSince=()=>{try{const at=localStorage.getItem(SIGNED_OUT_AT)||'';return at!==ownSignOutAt&&Number(at)>=pageLoadedAt;}catch{return false;}};
const sessionChannel=typeof BroadcastChannel==='undefined'?null:new BroadcastChannel('fmuc-session');
sessionChannel?.addEventListener('message',e=>{if(e.data==='signing-out'&&me)void signOut(false);});
async function signOut(tellTabs=true){
  if(signingOutNow())return;  // a second click: the sign-out under way answers it
  forgetSession();void signedOut();clearPrefs();try{localStorage.removeItem(ROTATED);}catch{/* nothing kept */}forgetPhoneContinue();clearTableViews();forgetRecent();forgetPanelWidths();forgetDeviceMemory();
  ownSignOutAt=String(Date.now());try{localStorage.setItem(SIGNED_OUT_AT,ownSignOutAt);}catch{/* a restored tab still asks the server */}
  history.replaceState(null,'',location.pathname+location.search);routeSeen=location.hash;
  const shell=el('main',{id:'main',class:'boot',tabindex:-1,'aria-busy':'true'},bootEmblem(),el('h1',{},'Signing out…'));
  root.replaceChildren(shell);shell.focus();documentTitle('Signing out');
  const result=logout();if(tellTabs)sessionChannel?.postMessage('signing-out');
  const answer=await result;
  if(answer.ended){await showLogin();if(answer.endSession)location.assign(answer.endSession);return;}
  const again=button('Try again',()=>void signOut(),'primary');
  shell.removeAttribute('aria-busy');shell.classList.add('boot-failed');documentTitle('Not signed out');
  shell.replaceChildren(bootEmblem(),el('h1',{},'Couldn’t sign you out'),el('p',{role:'alert'},'You may still be signed in on this device.'),el('div',{class:'actions'},again));
  again.focus();
}
// A page restored from the browser's back/forward cache missed the channel: signed out there, it starts afresh. Signed
// in, it asks the server once (raw fetch: no resume dialog over the old page); a sign-out on this device since the page
// loaded, an ended session or another account replaces the page with the sign-in screen. Still the same session: kept.
window.addEventListener('pageshow',e=>{if(!e.persisted)return;if(!me){location.reload();return;}
  const who=me.identity_id,toSignIn=()=>{history.replaceState(null,'',location.pathname+location.search);routeSeen=location.hash;void showLogin();};
  if(signedOutSince()){toSignIn();return;}
  void fetch('/api/me',{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}}).then(async r=>{
    if(me?.identity_id!==who)return;  // the page moved on meanwhile
    if(r.status===401||r.status===403||r.ok&&(await r.json()).identity_id!==who||signedOutSince())toSignIn();}).catch(()=>{/* no answer: the periodic check decides */});});
function shortcutsDialog(){
  showDialog('Keyboard shortcuts','',body=>body.append(table(['Shortcut','Action'],[
    [el('kbd',{},'Ctrl K'),'Search pages and actions'],[el('kbd',{},FEEDBACK_KEY),'Send feedback about this page'],[el('kbd',{},'?'),'Show this list'],[el('kbd',{},'Esc'),'Close a menu, panel or dialog'],
    [el('kbd',{},'Ctrl Z'),'Undo the last change'],[el('kbd',{},MESSAGES_KEY),'Go to the newest message and its Undo (Esc goes back)'],[el('kbd',{},'Ctrl Shift Z'),'Redo what you just undid (also Ctrl Y)'],
    // Schedule keys, named as its views are.
    ...(allowed('schedule')?SCHED_VIEWS.map(v=>[el('kbd',{},v.letter),`Schedule: ${v.label} view`]):[]),
    ...(allowed('schedule')?[[el('kbd',{},'J'),'Fix list focus: next cause'],[el('kbd',{},'K'),'Fix list focus: previous cause']]:[])])));
}
type About={version:string;release:string;commit:string|null;build_date:string|null;image_digest:string|null;dev_build?:string|null;deployment:string;migration:string;
  engines:Record<'compiler'|'validator'|'greedy'|'score'|'fairness',string>;worker:{release:string|null;last_seen_at:string}|null};
const reloadToUpdate=()=>location.reload();
const WEB_RELEASE=APP_VERSION+(WEB_COMMIT?`+${WEB_COMMIT}`:'');
/** What is running (GET /api/about): one image carries the API, worker and web client, so any difference is a stale
 *  page or a worker still on another image during a deploy. Builds compare as version+commit (two DEV builds of one
 *  version differ); this bundle's comes from APP_VERSION (generated.ts) and WEB_COMMIT (stamped at build). */
function aboutDialog(){
  const {dialog}=showDialog(`About ${brand().short_name}`,'',body=>{
    body.append(skeleton('lines'));
    get<About>('/api/about').then(a=>{
      const when=(iso:string|null)=>iso?`${formatDate(iso,zone())} ${formatTime(iso,zone())}`:'Not recorded';
      const workerRelease=a.worker?(a.worker.release||'release not reported'):'not seen yet';
      const stale=WEB_RELEASE!==a.release;
      const problems=[stale?`This page is ${WEB_RELEASE}; the server runs ${a.release}`:'',
        !a.worker?`The worker has not been seen yet; the server runs ${a.release}`
          :!a.worker.release?`The worker has not reported its release; the server runs ${a.release}`
          :a.worker.release!==a.release?`The worker runs ${workerRelease}; the server runs ${a.release}`:''].filter(Boolean);
      const facts:[string,string][]=[['Commit',a.commit||'Not recorded'],['Built',when(a.build_date)],
        ['Image',a.image_digest||'Not recorded'],['Environment',a.deployment==='production'?'Production':'Test'],['Database schema',a.migration],
        ['Web page',WEB_RELEASE],['Worker',a.worker?`${workerRelease} · seen ${when(a.worker.last_seen_at)}`:'Not seen yet'],
        ['Solver',a.engines.compiler],['Validator',a.engines.validator],['Greedy fill',a.engines.greedy],['Score',a.engines.score],['Fairness',a.engines.fairness]];
      const copy=button('Copy details',async()=>{
        try{await navigator.clipboard.writeText(`${brand().short_name} ${a.release}\n`+facts.map(([k,v])=>`${k}: ${v}`).join('\n'));notify('Details copied.');}
        catch{notify('Copying is blocked here. Select the details and press Ctrl+C.','info');}});
      body.replaceChildren(el('p',{class:'about-release'},a.version),
        a.dev_build?el('p',{class:'muted about-dev-build'},a.dev_build):'',
        el('div',{class:'about-status'},...(problems.length?problems.map(p=>badge(p,'warn','',{icon:'alert'})):[badge('Frontend and backend run the same release','ok','',{icon:'check'})])),
        stale?el('div',{class:'about-actions'},button('Reload to update',reloadToUpdate,'primary')):'',
        el('dl',{class:'request-facts about-facts'},...facts.flatMap(([k,v])=>[el('dt',{},k),el('dd',{},v)])),
        el('div',{class:'about-actions'},copy));
    },e=>body.replaceChildren(note((e as Error).message,'error',e)));
  });
  dialog.classList.add('about-dialog');
}
// The rule catalogue (admin flag, off by default) is the Rules page; whoever the server lets read rules may read it.
const ruleCatalogueOn=()=>!!(me?.department as Row|undefined)?.display_settings?.rule_catalogue;
function applyDisplay(){if(me){setTableAccount(me.identity_id,me.department.id);setWeekStart(me.organisation.week_start,me.organisation.timezone);setWeekend((me.department as Row|undefined)?.weekend_days);setLeaveDisplay((me.department as Row|undefined)?.leave_reason_display,me.person_id);if(!catalogues.assignment_types)setTypeColours(me.duty_types||[]);}const d=(me?.department as Row|undefined)?.display_settings||{};
  Object.assign(paths.rules,{title:'Rules',roles:[ruleCatalogueOn()?CAPABILITY.RULES_READ:CAPABILITY.RULES_PAGE]});/* B128: the contract's role lists, never a copy */setDepartmentNames(d.names_default);setDepartmentProperties(d.person_labels_enabled===true&&Array.isArray(d.properties)&&d.properties.length?d.properties as ViewProperty[]:null);codeRules.ignoreAl=d.ignore_al_prefix!==false;codeRules.unique=d.avoid_duplicate_codes!==false;
  // The department's title and particle lists (D75, display settings), else the contract's defaults.
  codeRules.titles=[...(Array.isArray(d.honorifics)?d.honorifics as string[]:HONORIFIC_DEFAULTS)];codeRules.particles=[...(Array.isArray(d.name_particles)?d.name_particles as string[]:NAME_PARTICLE_DEFAULTS)];forgetNames();}
/** Names of pages that were merged, so Ctrl K still finds them. */
const OLD_NAMES:Record<string,string>={home:'overview today dashboard',requests:'availability leave wishes swaps shift swap absence',people:'directory contacts eligibility',
  'insights/fairness':'fairness','insights/workload':'reports workload hours','insights/exports':'reports export csv calendar subscription','access/routes':'governance delegation review routes approvals',
  'system/imports':'data import spreadsheet','system/operations':'operations service health outbox','system/audit':'audit history log'};
function openCommandMenu(){
  if(!me)return;
  const account=sessionKey(),planner=permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)&&desktopNow();
  let directory:Promise<Row[]>|null=null,recent:Promise<Row[]>|null=null;
  openPalette(commandItems(),async query=>{
    // Staff can search contacts before visiting People. Only the directory projection is read for that role.
    // A person's record (its side panel) is desktop work: on a phone People opens only as Where is everyone (M29).
    const people=!allowed('people')||!desktopNow()?Promise.resolve([] as Row[]):holdsLists(catalogues)?Promise.resolve(rows('people')):(directory||=background<{people:Row[]}>('/api/directory').then(r=>r.people).catch(e=>{directory=null;throw e;}));
    const name=query.replace(/^(?:auto[ -]?schedule|rotas?)\s+/i,'').replace(/^fill \d+ open places in /i,'');
    // Keep the recent page only for this open menu: broad action keywords still find its draft actions.
    const latest=!planner?Promise.resolve([] as Row[]):(recent||=background<{workspaces:Row[]}>('/api/workspaces?limit=50&offset=0').then(r=>r.workspaces).catch(e=>{recent=null;throw e;}));
    const found=planner&&name?background<{workspaces:Row[]}>(`/api/workspaces?limit=50&offset=0&q=${encodeURIComponent(name)}`).then(r=>r.workspaces):latest;
    const [persons,matches,defaults]=await Promise.all([people,found,latest,ensureCatalogues()]);
    const rotas=[...matches,...defaults.filter(w=>!matches.some(m=>m.id===w.id))];
    return account===sessionKey()&&me?commandItems(rotas,persons):[];
  });
}
/** An explicit result owns its rota, days and version; a previous planner view or draft cannot change the destination. */
function openPaletteRota(w:Row,auto=false){
  if(!leaving(()=>openPaletteRota(w,auto),labelOf(w)))return;
  const published=!LIVE.includes(w.status);
  const query=new URLSearchParams({edit:published?'none':w.id});if(published)query.set('source',String(w.publication_id));
  setRoute(routeHash('schedule',['week',String(w.starts_on)],query));applyScheduleRoute();schedulingSettings.open=auto;
  history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;void renderPage();
}
function commandItems(rotas:Row[]=[],people:Row[]=holdsLists(catalogues)?rows('people'):directoryRows||[]){
  const items:PaletteItem[]=[],inPalette=(id:string)=>allowed(id)&&phoneNav(id);
  for(const [,ids] of [...navGroups,['Account',['inbox']] as [string,string[]]])for(const id of ids.filter(inPalette))items.push({group:'Pages',label:navLabel(id),icon:navIcons[id]||'grid',keywords:paths[id].title+' '+(paths[id].help||'')+' '+(OLD_NAMES[id]||'')+(PAGE_TABS[id]&&tabsFor(id).length===1?' '+tabsFor(id)[0].label+' '+(OLD_NAMES[`${id}/${tabsFor(id)[0].key}`]||''):''),run:()=>navigate(id)});
  // Each tab of a merged page by its own name ("Audit log", "Review routes"); old page names find their new home.
  // A page with one tab for this person is that tab: listing both would name the same place twice.
  for(const [id,list] of Object.entries(PAGE_TABS))if(inPalette(id)&&tabsFor(id).length>1)for(const t of tabsFor(id))items.push({group:'Pages',label:t.label,hint:navLabel(id),icon:navIcons[id]||'grid',keywords:`${t.label} ${OLD_NAMES[`${id}/${t.key}`]||''}`,run:()=>navigate(`${id}/${t.key}`)});
  if(inPalette('configuration')&&configSections().includes('setup'))items.push({group:'Pages',label:'Setup guide',hint:'Configuration',icon:'star',keywords:'setup getting started checklist',run:()=>navigate('configuration/setup')});
  // Each Configuration section is reachable by name (e.g. "duty types", "auto-schedule", "timezone").
  for(const kind of !inPalette('configuration')?[]:configSections().filter(k=>k!=='setup'))items.push({group:'Pages',label:kindLabel(kind),hint:'Configuration',icon:'settings',
    keywords:'configuration settings '+purposeOf(kind)+(kind==='department'?' timezone week start name sign-in session':kind==='sign_in'?' login password institutional oidc sso provider':kind==='auto_schedule'?' scoring weights priorities tolerance wishes search':''),run:()=>navigate('configuration/'+kind)});
  // On a phone (phone scope) search offers only what phone pages do: no planning, setup or People results.
  const desk=desktopNow();
  if(desk&&permits(CAPABILITY.COORDINATOR))items.push({group:'Actions',label:'New rota',icon:'plus',keywords:'create plan draft period new rota new draft',run:()=>workspaceForm(ctx())});
  if(desk&&permits(CAPABILITY.COORDINATOR)&&scoreCheckOn())items.push({group:'Actions',label:scoreCheckTitle(),icon:'upload',keywords:'benchmark import department rota files compare score check',run:openScoreCheck});
  if(desk&&permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR))items.push({group:'Actions',label:TERMS.newAnnouncement,icon:'bell',keywords:'notice message tell staff notify broadcast',run:()=>void ensureCatalogues().then(()=>catalogueForm(ctx(),'department_content'),()=>{})});
  if(desk&&permits(CAPABILITY.COORDINATOR))for(const w of rotas.filter(r=>r.status==='draft')){
    items.push({group:'Actions',label:`${TERMS.autoSchedule} ${w.name}`,hint:'Schedule',icon:'sparkle',keywords:'auto schedule generate fill run',run:()=>openPaletteRota(w,true)});
    const open=w.health?.summary?.places_open;
    if(open)items.push({group:'Actions',label:`Fill ${plural(open,'open place')} in ${w.name}`,hint:'Schedule',icon:'plus',keywords:'gaps staff needed',run:()=>openPaletteRota(w)});
  }
  if(desk&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR))for(const w of rotas.filter(w=>LIVE.includes(w.status)||w.publication_id&&permits(CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR)))items.push({group:'Rotas',label:String(w.name),hint:`${statusLabel(w.status)} · ${formatDate(w.starts_on)} – ${formatDate(lastIncludedDate(w.ends_on))}`,icon:'calendar',keywords:'rota '+w.status,run:()=>openPaletteRota(w)});
  items.push({group:'Actions',label:isDark()?'Switch to light mode':'Switch to dark mode',icon:isDark()?'sun':'moon',keywords:'theme appearance dark light',run:()=>{setTheme(isDark()?'light':'dark');}});
  if(!phoneQuery.matches)items.push({group:'Actions',label:'Keyboard shortcuts',icon:'keyboard',run:shortcutsDialog});  // a phone has none
  // Ctrl K closes before this runs, so the picture shows the page, not the menu.
  items.push({group:'Actions',label:'Send feedback',hint:FEEDBACK_KEY,icon:'message',keywords:'report problem bug suggestion idea question confusing help',run:()=>openFeedback()});
  items.push({group:'Pages',label:'My feedback',hint:'Account',icon:'inbox',keywords:'feedback reports sent',run:()=>navigate('feedback')});
  items.push({group:'Actions',label:'Sign out',icon:'logout',run:()=>void signOut()});
  // Names are not identifiers: duplicates and shortened labels still open the exact permitted record.
  for(const p of inPalette('people')?people:[])items.push({group:'People',label:fullNameOf(p),hint:[holdsLists(catalogues)?shortCode(p):'',p.display_title||p.title||'',sectionLabel(p)].filter(Boolean).join(' · '),icon:'people',keywords:[holdsLists(catalogues)?shortCode(p):'',p.display_name||'',...(p.aliases||[]),p.title||'',p.directory_section||'',...((p.tags||[]) as string[])].join(' '),run:()=>navigate(routeHash('people',[],new URLSearchParams({open:p.id})))});
  // Named sources use the same permitted sections and form owner as their tables; searching never saves.
  for(const [page,kinds] of catalogueSources())if(inPalette(page))for(const kind of kinds)for(const row of rows(kind))items.push({group:navLabel(page),label:(kind==='tags'?'#':'')+labelOf(row),
    hint:[kindLabel(kind),row.description||'',row.active===false?'Inactive':'',row.starts_on?formatDate(row.starts_on):''].filter(Boolean).join(' · '),icon:navIcons[page]||'grid',
    keywords:[kindLabel(kind),row.description||'',kind==='services'?'clinic':''].join(' '),run:()=>navigate(routeHash(page,[addressOf(kind)],{open:row.id}))});
  // Rule lines carry who-conditions and notes: listed only where the Rules page is (rules.read, applyDisplay).
  if(inPalette('rules')&&ruleCatalogueOn()){
    const seen=new Set<string>();
    for(const policy of rows('policies')){const rule=policy.definition?.rule;
      if(policy.status!=='active'||rule?.kind!=='catalogue'||seen.has(rule.id))continue;seen.add(rule.id);
      items.push({group:'Rules',label:labelOf(policy),hint:'Current rule',icon:navIcons.rules,keywords:policy.description||'',run:()=>navigate(routeHash('rules',[rule.id]))});
      for(const [i,line] of (rule.lines||[]).entries())items.push({group:'Rules',label:`${labelOf(policy)} · line ${i+1}`,hint:line.note||'Current line',icon:navIcons.rules,
        keywords:line.note||'',run:()=>navigate(routeHash('rules',[rule.id,line.id]))});
    }
  }
  return items;
}
/** Change view keeping the same anchor day in view (Week → Month shows the month holding that week). */
function switchView(v:SchedView){schedView=v;viewRange='';pickedDay='';}
/** The view's range: Year, Month and Day calendars have their own; the others may choose one. */
const rangeNow=():SchedRange=>['year','month','day'].includes(schedView)?VIEW_RANGE[schedView]:viewRange||VIEW_RANGE[schedView];
// View settings: a layout over a range. Calendar is the Day, Week or Month view by its range.
const LAYOUT_NAMES:[string,string][]=[['calendar','Calendar'],['coverage','Coverage'],['list','List']];
// The Calendar's Rows and Columns say what it shows; Coverage and List say it in one line.
const LAYOUT_HINTS:Record<string,string>={coverage:'Places filled against places needed, per duty and day.',list:'One row per session.',year:'Every day of the year by health.'};
/** The days in view: the range around the anchor, from the view's week start, without the weekdays it hides. */
function periodNow(anchor:string,ws:number):Period{
  const p=periodFor(rangeNow(),anchor,viewDays.week_start??ws,viewDays.days);
  if(viewDays.weekdays&&schedView!=='year'){const keep=new Set(viewDays.weekdays),days=p.days.filter(d=>keep.has((new Date(d+'T12:00:00Z').getUTCDay()+6)%7));if(days.length)p.days=days;}
  return p;
}
/** The renderer a view uses: the Calendar's three (duties, people with codes, people with cards), Coverage or List. */
const rendererOf=(v:SchedView)=>['week','day','month'].includes(v)?'calendar':v;
/** Calendar is one layout (days as columns) whose rows are duties or people; Coverage and List are their own. */
const layoutOf=(v:SchedView)=>['week','day','month','timeline','people'].includes(v)?'calendar':v;
/** The Calendar's rows and, for people, what a cell shows (the last people cells chosen are kept when rows switch back). */
let peopleCells:'codes'|'cards'='codes';
/** The Calendar's axes (View settings › Rows, Columns, Cells show; the toolbar's Group › Then by). Rows are the Week
 *  grid's duties, the People grid's people, or any dimension in the Schedule grid (the Timeline view); columns are days,
 *  weeks, weekdays, months or a dimension. The Schedule grid draws everything but day columns of cards. */
/** Coverage's own rows and columns (D80; saved as PlannerViewSettings.coverage): never another layout's. The default
 *  (duty types over days) keeps the Coverage table. */
let coverRows:Dim='type',coverColumns:GridColumn='day';
let gridRows:Dim='person',gridColumns:GridColumn='day',gridCells:'cards'|'codes'|'counts'='cards',gridSubgroup:Dim|'none'='none';
const rowsNow=():'duty'|Dim=>['week','day','month'].includes(schedView)?'duty':schedView==='people'?'person':gridRows;
/** What a cell shows: sessions (duty rows) or duties and hours (People) as cards, duty codes (the Timeline) or counts. */
const cellsNow=()=>schedView==='people'?(gridCells==='counts'?'counts':'cards'):gridCells==='counts'?'counts':schedView==='timeline'?'codes':'cards';
const useGrid=()=>schedView==='timeline'||['week','month','day','people'].includes(schedView)&&(gridColumns!=='day'||cellsNow()==='counts')
  // Coverage by other rows or columns: places filled / needed in the same counts grid (one renderer, one count).
  ||schedView==='coverage'&&(coverRows!=='type'||coverColumns!=='day');
/** The grid's first grouping: the People view's own (rank, tag; supervision teams are the People grid's), else the view's. */
const groupNow=():Dim|'none'=>schedView==='people'?(peopleView.group==='rank'||peopleView.group==='tag'?peopleView.group:'none'):weekView.groupBy;
function setLayoutRange(layout:string,range:SchedRange){
  if(layout==='people')peopleCells='cards';else if(layout==='timeline'&&gridRows==='person'&&gridCells!=='counts')peopleCells='codes';
  if(layout!=='timeline'&&weekView.groupBy!=='none'&&!SESSION_GROUPS.includes(weekView.groupBy))weekView.groupBy='service';  // groups of people: the Schedule grid's only
  if(range==='year')range=VIEW_RANGE[layout==='calendar'?'week':layout as SchedView];
  if(layout==='calendar'){schedView=range==='day'?'day':range==='month'?'month':'week';viewRange=['2weeks','3days','custom'].includes(range)?range:'';}
  else{schedView=layout as SchedView;viewRange=range===VIEW_RANGE[schedView]?'':range;}
  pickedDay='';
}
const typing=(t:EventTarget|null)=>t instanceof HTMLElement&&(t.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(t.tagName));
document.addEventListener('keydown',e=>{
  if(!me)return;
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openCommandMenu();return;}
  if(typing(e.target)||e.ctrlKey||e.metaKey||e.altKey||document.querySelector('dialog[open]'))return;
  if(e.key==='?'){e.preventDefault();shortcutsDialog();}
  else if(view==='schedule'&&!e.shiftKey){const next=SCHED_VIEWS.find(v=>v.letter===e.key.toUpperCase());if(next&&next.key!==schedView){plannerViewId='';switchView(next.key);void redraw();}}
});

let lastSaved=0,saveError='',saveUnconfirmed=false;
const savedText=()=>{const s=Math.round((Date.now()-lastSaved)/1000);return s<10?'Saved just now':s<60?`Saved ${s}s ago`:`Saved ${Math.round(s/60)} min ago`;};
/** Footer general status is icons when all is well (the words are the tooltip and accessible name); saving, a failed
 *  save and offline also show a word. Save: ✓ saved, a spinner while saving, red when the last save failed. */
function paintSave(){
  const n=document.querySelector<HTMLElement>('#save-state');if(!n)return;
  // D-4: only this person's own saves: nothing is said until one starts (no "Saved" on opening a rota).
  // maintenance: an unknown outcome is never "Not saved": it is checked (api.ts confirmingNow), or said as not confirmed.
  const saving=confirmingNow()||ownSavingNow(),busy=savingWords(),
    failed=saveUnconfirmed?'Couldn’t confirm':'Not saved',text=saving?busy:saveError?(saveUnconfirmed?saveError:'Not saved: '+saveError):lastSaved?savedText():'';n.hidden=!text;
  // Hidden, it has no name or tip either (nothing to say, and never what is obvious).
  if(!text){n.removeAttribute('aria-label');delete n.dataset.tip;n.replaceChildren();return;}
  n.className='save-state status-icon'+(saving?' saving':saveError?' error':lastSaved?' saved':'');
  n.setAttribute('aria-label',text);n.dataset.tip=text;
  // A change of state (saving, a failed save) is said in words on screen too, not only by the icon (G-04).
  n.replaceChildren(saving?el('span',{class:'spinner','aria-hidden':'true'}):icon(saveError?'alert':'check'),el('span',{class:saving||saveError?'status-word':'sr-only'},saving?busy:saveError?failed:text));
}
/** Footer, right: Send feedback, one press from any page (the page is captured as it is). */
function feedbackButton(){
  const b=iconButton('Send feedback','message',()=>openFeedback(),{class:'bare status-icon feedback-button','data-tip':`Send feedback · ${FEEDBACK_KEY}`});
  return b;
}
/** Footer, left: "Updating…" while a background read is slow (api.ts updatingNow): neutral, never the connection's. */
const paintUpdating=(n:HTMLElement)=>{const on=updatingNow();n.hidden=!on;
  n.replaceChildren(...(on?[el('span',{class:'spinner','aria-hidden':'true'}),el('span',{},UPDATING_WORDS)]:[]));};
function updatingState(){const node=el('span',{class:'footer-updating',role:'status'});paintUpdating(node);return node;}
// A save starting or ending hands the word back and forth with the save state (updatingNow).
for(const t of ['reads-updating','rota-saving','rota-saved','rota-save-settled'])window.addEventListener(t,()=>document.querySelectorAll<HTMLElement>('.footer-updating').forEach(paintUpdating));
function saveState(){
  const node=el('span',{class:'save-state status-icon',id:'save-state',role:'status','aria-live':'polite'});
  queueMicrotask(paintSave);return node;
}
/** The one connection indicator (footer, right): a dot; the full words are its tooltip and accessible name, and a
 *  change of state is said on screen in words (G-04: offline, or a server error; D-1: reconnecting; never for a slow answer, maintenance). */
// `error`: why the server could not be reached; a server error (5xx) is named as such, not as being offline; a gateway
// error (502–504: a restart or an overloaded server) is reconnecting, as is no answer while the device is online.
function showConnection(ok:boolean,error?:unknown){
  connected=ok;serverDown=!ok&&error instanceof ApiError&&error.status>=500&&![502,503,504].includes(error.status);
  // A successful connection check clears the stale banner, unless the last refresh itself failed (Retry, or the next
  // successful read of the page, clears that).
  if(!ok)showStale(true,error);else if(!refreshFailed)showStale(false);
  paintConnection();
}
let serverDown=false,refreshFailed=false;
function paintConnection(){
  const c=document.querySelector<HTMLElement>('#connection');if(!c)return;
  const health=serverHealth(),waiting=!connected&&!serverDown&&navigator.onLine||health==='down';
  const [word,text]=!connected&&!navigator.onLine?['Offline','Offline · retrying']:!connected&&serverDown?['Server error','Server error · retrying']
    :waiting?['Reconnecting…','Reconnecting… · the server is not answering']:['','Connected'];
  c.className='connection status-icon'+(!word?'':waiting?' offline busy':' offline');c.setAttribute('aria-label',text);c.dataset.tip=text;
  c.replaceChildren(...(waiting?[el('span',{class:'spinner','aria-hidden':'true'})]:[]),el('span',{class:word?'status-word':'sr-only'},word||text));
}
// D-1: every request reports the server's health (api.ts): the footer follows it, and when the server answers again
// after it stopped, the page reads afresh by itself (a page that failed to load, or data shown while it was down).
// A read that got no answer is an outage only when the app's own probe (the pulse) gets none either: one unreachable
// read beside a server that answers is that read's failure, said with its own Retry. Taking it for an outage made
// every next answer "the server is back" and refreshed the page, which asked for that read again, without end.
window.addEventListener('server-health',e=>{const now=(e as CustomEvent).detail;
  // An outage is declared only when the pulse fails too (LABEL-FE: one endpoint refusing is not the server down), and
  // after it only what failed is read again (a page that did not load or could not refresh), never in a loop.
  if(now==='ok'&&wasDown&&me){wasDown=false;showConnection(true);if(refreshFailed||pageFailed)void refresh(true);return;}
  if(now==='down'&&!wasDown&&!probing&&me){probing=true;background('/api/pulse').then(()=>{},e=>{wasDown=true;
    // Confirmed down: rows on screen say at once that they could not be refreshed (not only the footer), and are read again when it is back.
    if(!sessionChanged(e)){refreshFailed=true;showConnection(false,e);}}).finally(()=>{probing=false;});}
  paintConnection();});
let wasDown=false,probing=false,pageFailed=false;
/** A refresh failed (a server error or offline): a page still showing data says, once, how old it is, with Retry
 *  (and, for a server error, its message and Report this problem). Shared by every page (it sits above the page's own
 *  header and toolbar, which stay); any successful read of the page clears it. A page that showed nothing shows
 *  readFailed() instead. */
let stale=false,staleError:unknown=null,freshAt=Date.now(),staleBanner:{text:string;node:HTMLElement}|null=null;
function showStale(on:boolean,error?:unknown){
  if(!on){stale=false;staleError=null;freshAt=Date.now();}else{if(!stale)staleError=null;stale=true;if(error)staleError=error;}
  const main=document.querySelector<HTMLElement>('#main');main?.querySelector(':scope>.stale-banner')?.remove();
  if(!stale||!main?.hasAttribute('data-loaded-view')||['restricted','desktop-only'].includes(main.dataset.loadedView||''))return;
  // One line (N-11): what failed and how old the data is; a server's own words (errorWords) follow it as text (read on
  // touch and by keyboard too) with Report this problem (lost connection: nothing for the team to fix, so no report, N-10).
  // Worded again when offline becomes a server error.
  const server=staleError instanceof ApiError&&staleError.status>=500,said=server?errorWords(staleError):'';
  const text=`Couldn’t refresh · showing data from ${formatTime(new Date(freshAt).toISOString(),zone())}`,key=text+(server?' · '+said:'');
  // The same banner is put back on each redraw: its error is recorded (feedback's recent errors) once, not per draw.
  if(staleBanner?.text!==key){const banner=note(text,server?'error':'warning',server?staleError:undefined);banner.classList.add('stale-banner');banner.setAttribute('role','status');
    if(said)banner.querySelector(':scope>span')?.append(' · '+said);
    banner.prepend(icon('alert'));banner.append(button('Retry',retryRefresh,'secondary small'));staleBanner={text:key,node:banner};}
  main.prepend(staleBanner.node);
}
// Rota checks and automatic read markers must not replace the person's save status (api.ts automatic, D-4).
const ownSave=(e:Event)=>{const d=(e as CustomEvent).detail||{};return !automatic(d.kind,d.payload);};
window.addEventListener('rota-saving',e=>{if(ownSave(e))paintSave();});
window.addEventListener('rota-save-settled',()=>paintSave());  // any refusal ends "Saving…" (a refused Undo says no more)
window.addEventListener('rota-save-checking',e=>{if(ownSave(e))paintSave();});
// A save confirmed after an unknown outcome: the page reads the rota again, so it shows what the server has.
window.addEventListener('rota-saved',e=>{if(!ownSave(e))return;const d=(e as CustomEvent).detail;
  // A held Apply wrote nothing: "Saving…" ends, but nothing is called saved.
  if(d?.kind==='generation.apply'&&!applyWrote(d.result)){paintSave();return;}
  lastSaved=Date.now();saveError='';saveUnconfirmed=false;saveErrorIn=null;paintSave();
  if((e as CustomEvent).detail?.replayed)void refresh(true);});
window.addEventListener('rota-recheck',()=>void refresh());
// A save refused inside a dialog is said there; once that dialog closes (discarded) nothing is left unsaved (SW3-C-05).
let saveErrorIn:Element|null=null;
window.addEventListener('rota-save-failed',e=>{if(!ownSave(e))return;saveError=String((e as CustomEvent).detail?.message||'the last change failed');
  // Not confirmed is not "nothing left unsaved": closing the dialog does not clear it, only a save or a page change.
  saveUnconfirmed=(e as CustomEvent).detail?.code==='SAVE_UNCONFIRMED';
  const within=saveErrorIn=saveUnconfirmed?null:[...document.querySelectorAll('dialog[open]')].at(-1)||null;paintSave();
  within?.addEventListener('close',()=>{if(saveErrorIn===within){saveError='';saveErrorIn=null;paintSave();}},{once:true});});
window.setInterval(()=>{if(lastSaved&&!saveError)paintSave();},15000);
// The automatic check that stopped says so itself, above the grid with its cause (B7 failed): no second message.
// A refused rota decision carries the rota as it is now (api.ts): the page redraws in that state, and the dialog that
// asked says what happened (dom.ts settledWords), so no second, generic message.
window.addEventListener('rota-conflict',e=>{const d=(e as CustomEvent).detail;if(!d?.current)return;
  if(!patchDraft({workspace:d.current} as unknown as Row,d.kind)){forgetSchedule();inPlace=false;}savedKinds.add(d.kind);void refresh();});
// G-04: the review-stage words are for a rota decision (submit, approve, publish) only; a refused draft edit says its own.
// The one generic message for a refused change, said once: never when a form shows the server's words itself (owned,
// api.ts ownErrors) or a refused rota decision says what happened (current, settledWords). Its advice fits the code.
window.addEventListener('rota-conflict',e=>{const d=(e as CustomEvent).detail||{};
  if(d.current||d.owned||d.code==='STALE_INPUTS'&&(autoChecking||!ROTA_DECISIONS.has(d.kind)))return;
  if(d.code==='STALE_INPUTS'){notify('Rules, people or availability changed since this rota was checked, so it cannot go ahead as it is. It needs to be checked again (a coordinator reopens and resubmits it).','error');return;}
  // An Assign is bound to the fingerprint its candidates were checked under (PERF async fill-options): its stale is theirs.
  if(d.code==='STALE_PREVIEW'&&d.kind==='shift.update'){notify('Who can work this session changed since it was checked, so nothing was assigned. Check who can work it again.','error');return;}
  if(d.code==='STALE_PREVIEW'){notify('The impact preview is out of date, so nothing was saved. Refresh the preview, then apply it again.','error');return;}
  notify('This record changed while you were working. Your change was not saved.','error',{label:'Show the latest',run:()=>refresh()});});

/** The app frame (sidebar, top bar, footer) is built once per account and kept across pages: a page change only marks
 *  its nav item, puts back its title and empty action slot, and swaps in a fresh #main (SH-07). */
let frame:{key:string;root:HTMLElement;nav:HTMLElement;drawer:(open:boolean,back?:boolean)=>void;sidebar:HTMLElement;top:HTMLElement;actions:HTMLElement;footer:HTMLElement}|null=null;
const pageTitle=()=>el('div',{class:'department-title page-title'},el('h1',{title:paths[view]?.title||''},paths[view]?.title||''),paths[view]?.help?pageInfo(paths[view].help!):null);
const pageInfo=(text:string)=>el('button',{type:'button',class:'icon-button bare page-info','aria-label':'About this page','data-help':'','data-tip':text},icon('help'));
function shell(){
  if(!me)return;
  const key=JSON.stringify([me.identity_id,me.display_name,me.roles,me.department.id,me.department.name,me.deployment,me.version,navGroups.map(([,ids])=>ids.filter(inNav))]);
  if(frame?.key===key&&frame.root.isConnected){
    const f=frame;
    f.nav.querySelectorAll<HTMLElement>('[data-route]').forEach(b=>{const on=b.dataset.route===view;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    f.drawer(false);f.sidebar.classList.remove('peek');
    f.top.classList.remove('has-planner');f.top.querySelector('.department-title, .planner-bar')?.replaceWith(pageTitle());f.actions.replaceChildren();
    f.footer.querySelector('.page-status')?.replaceChildren();
    document.querySelector('#main')?.replaceWith(el('main',{id:'main',class:'page',tabindex:-1}));
    paintConnection();trackStatusFooter(f.footer);applyNavBadges();return;
  }
  const nav=el('nav',{id:'department-nav',class:'nav','aria-label':'Department pages'});
  for(const [group,ids] of navGroups){
    const entries=ids.filter(inNav);
    if(!entries.length)continue;
    nav.append(el('div',{class:'nav-group','aria-label':group},el('span',{class:'nav-group-label'},group),...entries.map(id=>{
      const b=button(navLabel(id),()=>{drawer(false);navigate(id);},'ghost',{'aria-current':view===id?'page':undefined,class:view===id?'active':'','data-route':id});
      return withIcon(b,navIcons[id]||'grid');
    })));
  }
  const pinned=navPinned();
  const pin=iconButton(pinned?'Unpin sidebar':'Pin sidebar open','pin',()=>{setNavPinned(!navPinned());shellRoot.dataset.nav=navPinned()?'pinned':'rail';pin.setAttribute('aria-pressed',String(navPinned()));const l=navPinned()?'Unpin sidebar':'Pin sidebar open';pin.setAttribute('aria-label',l);pin.setAttribute('data-tip',l);},{class:'bare pin-button','aria-pressed':String(pinned)});
  const searchTrigger=el('button',{type:'button',class:'search-trigger','aria-label':'Search or jump to'},icon('search'),el('span',{},'Search or jump to…'),el('kbd',{},'Ctrl K'));
  searchTrigger.addEventListener('click',openCommandMenu);
  const account=el('button',{type:'button',class:'account-button','aria-haspopup':'dialog','aria-expanded':'false','aria-label':'Account menu, '+me.display_name},
    avatar(me.display_name),el('span',{class:'who'},el('strong',{},me.display_name),el('small',{},me.roles.map(roleLabel).join(' · '))),icon('chevronUp'));
  account.addEventListener('click',()=>accountMenu(account));
  const sidebar=el('aside',{class:'sidebar','aria-label':'Navigation'},
    // The logo is the way home (a "+" read as a create button).
    // On a phone the drawer's head is the department and a close button (no pin: N-08).
    el('div',{class:'brand'},el('a',{href:'#home',class:'brand-home','aria-label':'Home · '+brand().app_name,'data-brand-home':''},brandEmblem(),el('div',{class:'brand-text','aria-hidden':'true'},el('strong',{'data-brand-name':''},brand().app_name),el('small',{},me.department.name))),pin,
      iconButton('Close menu','close',()=>drawer(false,true),{class:'bare drawer-close'})),
    searchTrigger,nav,el('div',{class:'sidebar-bottom'},account));
  // Arm from pointer movement: replacing the shell under a stationary pointer fires mouseenter again after navigation.
  // A deliberate hover still opens the rail; leaving or clicking closes it.
  let peekTimer=0;
  const closePeek=()=>{window.clearTimeout(peekTimer);peekTimer=0;sidebar.classList.remove('peek');};
  sidebar.addEventListener('mousemove',()=>{if(shellRoot.dataset.nav==='rail'&&!peekTimer&&!sidebar.classList.contains('peek'))peekTimer=window.setTimeout(()=>{peekTimer=0;if(sidebar.isConnected&&sidebar.matches(':hover')&&shellRoot.dataset.nav==='rail')sidebar.classList.add('peek');},320);});
  sidebar.addEventListener('mouseleave',closePeek);
  sidebar.addEventListener('click',closePeek);
  sidebar.addEventListener('focusin',e=>{if(shellRoot.dataset.nav==='rail'&&(e.target as HTMLElement).matches(':focus-visible'))sidebar.classList.add('peek');});
  sidebar.addEventListener('focusout',e=>{if(!sidebar.contains(e.relatedTarget as Node)&&!sidebar.matches(':hover'))sidebar.classList.remove('peek');});
  // Escape folds a rail opened by focus and hands focus to the page (N-22).
  sidebar.addEventListener('keydown',e=>{if(e.key==='Escape'&&sidebar.classList.contains('peek')&&!sidebar.classList.contains('navigation-open')){e.stopPropagation();closePeek();document.querySelector<HTMLElement>('#main')?.focus({preventScroll:true});}});
  // Phone menu: a modal drawer over a scrim. Opening moves focus into it and makes the page behind inert; a tap on the
  // scrim, its close button or Escape closes it and returns focus to the menu button (N-03).
  // Open, it is a modal dialog named Menu (maintenance, WCAG 4.1.2); closed, the navigation landmark again.
  const drawer=(open:boolean,back=false)=>{sidebar.classList.toggle('navigation-open',open);menu.setAttribute('aria-expanded',String(open));
    if(open)Object.entries({role:'dialog','aria-modal':'true','aria-label':'Menu'}).forEach(([k,v])=>sidebar.setAttribute(k,v));
    else{sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');sidebar.setAttribute('aria-label','Navigation');}
    const behind=sidebar.parentElement?.querySelector<HTMLElement>('.workspace-shell');if(behind)behind.inert=open;
    if(open)(nav.querySelector<HTMLElement>('[aria-current=page]')||nav.querySelector<HTMLElement>('button'))?.focus();else if(back)menu.focus();};
  const menu=iconButton('Menu','menu',()=>drawer(!sidebar.classList.contains('navigation-open')),{class:'mobile-menu','aria-controls':'department-nav','aria-expanded':'false'});
  sidebar.addEventListener('keydown',e=>{if(e.key==='Escape'&&sidebar.classList.contains('navigation-open')){e.stopPropagation();drawer(false,true);}});
  const scrim=el('div',{class:'nav-scrim','aria-hidden':'true'});scrim.addEventListener('click',()=>drawer(false,true));
  menu.append(el('span',{class:'sr-only'},'Menu'));
  const bell=iconButton('Inbox','bell',()=>notificationsMenu(bell),{class:'bare bell','aria-haspopup':'dialog'});
  const mobileAccount=el('button',{type:'button',class:'account-button mobile-account','aria-haspopup':'dialog','aria-expanded':'false','aria-label':'Account menu, '+me.display_name},avatar(me.display_name));
  mobileAccount.addEventListener('click',()=>accountMenu(mobileAccount));
  const actions=fitted(el('div',{class:'page-actions'}));
  const top=el('header',{class:'topbar'},menu,pageTitle(),actions,el('div',{class:'actions'},bell,mobileAccount));
  const content=el('main',{id:'main',class:'page',tabindex:-1});top.append(topProgress());
  // Production vs test must be unmistakable: test is gold and says so; production is named plainly (envBadge).
  const production=me.deployment==='production';
  const envText=(production?'Production: real rotas and people':'Test environment: test accounts only')+` · Release ${me.version}`;
  // An explicit landmark (live check): the status footer is "Status" (contentinfo) on every page, the Fix list page too,
  // whatever it ends up nested in.
  const footer=el('footer',{class:'app-footer',role:'contentinfo','aria-label':'Status'},updatingState(),el('div',{class:'page-status'}),
    el('div',{class:'service-status'},feedbackButton(),saveState(),el('span',{id:'connection',role:'status'}),
      envBadge(production,envText)));
  const shellRoot=el('div',{class:'shell','data-nav':pinned?'pinned':'rail'},sidebar,scrim,el('div',{class:'workspace-shell'},top,content,footer));
  const refocus=keepDock(shellRoot);root.replaceChildren(shellRoot);refocus();
  frame={key,root:shellRoot,nav,drawer,sidebar,top,actions,footer};
  paintConnection();
  trackStatusFooter(footer);
  applyNavBadges();
}
// Small counts on the sidebar and bell: what is waiting where. Loaded in the background, at most once a minute.
let navBadges:Record<string,{text:string;label:string}>={},badgesLoaded=0,badgesRun=0,badgesLoading=false;
/** A forced load asked for while one runs: run once more afterwards with what was asked (none named: every count). */
let badgesOwed:{saved?:string[];scopes?:Set<LiveScope>|null}|null=null;
function applyNavBadges(){
  document.querySelectorAll<HTMLElement>('#department-nav [data-route]').forEach(b=>{
    const words=`nav-badge-${b.dataset.route}`;
    b.querySelector('.nav-count')?.remove();document.getElementById(words)?.remove();const badge=navBadges[b.dataset.route!];b.classList.toggle('has-badge',!!badge);
    // The frame is kept between pages, so a count that has gone takes its tip and its words with it.
    // The words describe the button in every layout (aria-describedby: aria-description is still a draft): the collapsed
    // rail hides the number (a dot instead), and the visible number is decoration. The words are aria-hidden so they add
    // nothing to the name; a description by reference still reads them. Other ids (an open tooltip) are kept.
    const ids=(b.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!==words);
    if(badge){b.append(el('span',{class:'nav-count','aria-hidden':'true'},badge.text),el('span',{class:'sr-only','aria-hidden':'true',id:words},badge.label));
      b.setAttribute('data-tip',`${navLabel(b.dataset.route!)} · ${badge.label}`);ids.unshift(words);}
    else b.removeAttribute('data-tip');
    if(ids.length)b.setAttribute('aria-describedby',ids.join(' '));else b.removeAttribute('aria-describedby');
  });
  // The bell shows the capped number (99+); its name and tooltip say the exact one (unreadCount, maintenance).
  const bell=document.querySelector<HTMLElement>('.bell');if(bell){bell.querySelector('.count')?.remove();const n=navBadges.inbox;const name=n?`Inbox · ${n.label}`:'Inbox';
    if(n)bell.append(el('span',{class:'count','aria-hidden':'true'},n.text));bell.setAttribute('aria-label',name);bell.dataset.tip=name;}
}
// The commands each nav count can change (by prefix): after saves, only those counts are read again.
// What can move each count: the saves (by command kind) and the live changes (by scope).
const BADGE_SOURCES:Record<string,{kinds:string[];live:LiveScope[]}>={schedule:{kinds:['workspace.','shift.','draft.','generation.','series.','job.'],live:['workspace']},
  requests:{kinds:['request.','availability_pattern.','availability.','swap.','person.workload.','person.overtime.','calendar.protection.'],live:['requests','swaps','availability','rules']}};
/** The Schedule's count: draft sessions with open places (coordinators; each rota summary's sessions_with_gaps, a count of
 *  sessions, not places or causes), else rotas waiting for review (approvers). */
function scheduleBadge(rotas:Row[]):{text:string;label:string}|undefined{
  const open=rotas.filter(w=>w.status==='draft').reduce((a,w)=>a+(w.health?.summary?.sessions_with_gaps||0),0),waiting=rotas.filter(w=>w.status==='submitted').length;
  if(permits(CAPABILITY.COORDINATOR)&&open)return {text:open>99?'99+':String(open),label:`${plural(open,'session')} with open places in drafts`};
  if(permits(CAPABILITY.APPROVER)&&waiting)return {text:String(waiting),label:`${waiting} rota${waiting>1?'s':''} waiting for review`};
}
/** `list`: the rota list already being read (boot's read-ahead), so the counts never ask for it a second time. */
async function loadNavBadges(force=false,saved?:string[],scopes?:Set<LiveScope>|null,list?:Promise<RotaList>|null){
  if(me&&badgesLoading&&force){const all=(o:{saved?:string[];scopes?:Set<LiveScope>|null})=>!o.saved&&!o.scopes;
    badgesOwed=!badgesOwed?{saved,scopes}:all(badgesOwed)||all({saved,scopes})?{}
      :{saved:[...badgesOwed.saved||[],...saved||[]],scopes:new Set([...badgesOwed.scopes||[],...scopes||[]])};
    return;}
  if(!me||badgesLoading||(!force&&Date.now()-badgesLoaded<60000))return;
  const account=me,run=++badgesRun;
  // `saved`, `scopes`: the counts those saves and live changes cannot change are kept as they are.
  const some=!!saved||!!scopes;
  const reads=(badge:string)=>!some||!!saved?.some(k=>BADGE_SOURCES[badge].kinds.some(p=>k.startsWith(p)))||BADGE_SOURCES[badge].live.some(s=>scopes?.has(s));
  badgesLoading=true;
  try{
    const next:Record<string,{text:string;label:string}>=some?Object.fromEntries(Object.entries(navBadges).filter(([b])=>b in BADGE_SOURCES&&!reads(b))):{};
    const jobs:Promise<void>[]=[];
    if(reads('schedule')&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER))jobs.push((list||background<RotaList>('/api/workspaces?limit=50&offset=0')).then(r=>{rotaTotal=r.total;
      const b=scheduleBadge(r.workspaces);if(b)next.schedule=b;
    }).catch(()=>{}));
    // The dot on Requests counts what waits on you (requests to decide, weekly availability to approve, swaps to answer or prepare).
    if(reads('requests')&&permits(CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER))jobs.push(background<{counts:{to_decide:number}}>('/api/requests/all?status=waiting&limit=1').then(r=>{const n=r.counts.to_decide;if(n)next.requests={text:n>99?'99+':String(n),label:`${n} waiting for you`};}).catch(()=>{}));
    // The live pulse already carries the unread count; before the first pulse (or after marking read) the server's total.
    const unreadNow=seenUnread!==null?Promise.resolve(seenUnread):background<NotificationList>('/api/notifications?limit=1').then(r=>r.unread_total??0);
    jobs.push(unreadNow.then(n=>{if(n)next.inbox=unreadCount(n);}).catch(()=>{}));
    await Promise.all(jobs);
    // Setup progress on Configuration (admins; coordinators while it is their setup guide), after the rota count is read.
    if(permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR)){
      const counts=await setupCounts(ctx());
      if(counts){const steps=requiredSteps(counts),done=steps.filter(stepDone).length;if(done<steps.length)next.configuration={text:`${done}/${steps.length}`,label:`${done} of ${steps.length} setup steps done`};}}
    if(me!==account||run!==badgesRun)return;
    navBadges=next;badgesLoaded=Date.now();applyNavBadges();
  }finally{if(run===badgesRun){badgesLoading=false;const owed=badgesOwed;badgesOwed=null;if(owed)void loadNavBadges(true,owed.saved,owed.scopes);}}
}

/** Settings pages with Save (Department settings, Auto-schedule, Names) register whether they hold unsaved changes;
 * leaving asks first with the same Keep editing / Discard changes choice as a dialog (leaveGuard), naming where you were
 * going. Every page render clears it. A page may say what is unsaved itself (a rule's draft: "3 changes not yet
 * proposed. …"): that draft is kept, so the choice is Keep editing / Go to <page>. */
let leaveCheck:(()=>boolean|string)|null=null;
// SW3-D-09: within Configuration the guard names the section you were going to ("Tags"), not the page you are on.
const pageName=(target:string)=>{const [page,part]=target.replace(/^#/,'').split(/[?]/)[0].split('/');
  return page==='configuration'&&part?kindLabel(kindAt(part)):paths[page]?.title||'another page';};
// The docked item panel is asked last: it closes with its page (unless it holds unsaved changes).
function leaving(then?:()=>void,where='another page'){
  // A dialog with unsaved changes (an Approve note, a person, a rule) asks first: Back never discards it silently.
  if(askDirtyDialog(then))return false;
  // An unchanged record form closes at once; a changed one asks, and Discard changes closes it and goes on to `then`.
  if(catalogueSource){const source=catalogueSource;if(source.dialog.isConnected)source.tryClose();
    if(source.dialog.isConnected){if(then)source.tryCloseThen(()=>{source.close();then();});return false;}source.clear();}
  const dirty=leaveCheck?.();
  if(!dirty)return dockLeave(then);
  if(then)leaveGuard(where,()=>{leaveCheck=null;then();},typeof dirty==='string'?dirty:'',typeof dirty==='string');
  return false;
}
// Staged Sheet cells (the Bulk editor's and every list's Sheet) live only in this tab: a reload or close asks first.
window.addEventListener('beforeunload',e=>{if(leaveCheck?.()||sheetStagedCount())e.preventDefault();});
/** The address beyond the page: a tab or record (`#system/audit`) and a query (`#requests?type=swap`). Pages read them;
 * a tab switch writes them. Old addresses arrive through ROUTE_ALIASES (routes.ts). */
let routeParts:string[]=[],routeQuery=new URLSearchParams();
/** A link to no page here (a typo, an old bookmark) opens Home and says so once (N-21). */
let unknownLink=false;
/** A Schedule address naming no view: said by renderPage, after it clears the old page's messages. */
let routeNotice='';
function setRoute(target:string){
  routeShift='';shiftLink=null;  // a new address: no earlier link's session carries over
  const r=parseRoute(target,{today:toLocal(new Date().toISOString(),zone()).slice(0,10),planner:permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR),publication:publicationId}),known=r.page in paths;
  if(!known&&r.page)unknownLink=true;
  view=known?r.page:'home';routeParts=known?r.parts:[];routeQuery=known?r.query:new URLSearchParams();
}
/** Where Back steps: a page or one of its tabs (Schedule views, days and drafts stay one entry: O-6). */
// The Schedule's own pages (the Fix list, Benchmark) are steps too: Back from them returns to the rota (SW3-B-12).
const pageStep=()=>view+'/'+(PAGE_TABS[view]||view==='schedule'&&schedSubPage()?routeParts[0]||'':'');
// Each history entry knows its place in this tab ({step}), so a Back the leave check refuses is stepped back over
// (history.go) and the entry the person asked for stays where it was: Back again goes there. Address rewrites (tabs,
// filters, the Schedule's day) keep their entry's place.
// shortcut: history.replaceState is wrapped once so the many `replaceState(null,…)` address rewrites keep their entry's
// step; upgrade path: a named replaceAddress(url) helper that every caller uses.
// D-075: every address the app writes is the address it has seen (routeSeen), whichever caller wrote it: one write
// that did not update it left the Schedule's writeRoute refusing until the next navigation, keeping an old draft's edit=.
const replaceAddress=history.replaceState.bind(history);
history.replaceState=(state:unknown,unused:string,url?:string|URL|null)=>{replaceAddress(state??history.state,unused,url);if(url!=null)routeSeen=location.hash;};
const stepOf=():number|null=>typeof history.state?.step==='number'?history.state.step:null;
let shownStep=0;
/** The calendar link just made (shown once), or the note that it cannot be shown again: it outlives a live redraw of
 *  Exports (one follows calendar_feed.create), not a visit (openPage clears it). */
let feedShown:{path?:string;recovered:boolean}|null=null;
/** The connected Exports panel's draw: a reply to New link or Subscribe draws there, even if a live redraw replaced the panel that asked. */
let feedDraw:((next?:typeof feedShown)=>Promise<void>)|null=null;
/** A ?shift= link's session as it is now (Show on schedule from the Issues page, Home, a profile): said in the session's
 *  own words whenever the view moves for it, never a silent jump (Design Lead, f7). */
const SESSION_MOVED=(day:string)=>`This session moved to ${day}`,SESSION_REMOVED='This session was removed';
/** The session a ?shift= link marked, the open that drew it and the day it was on: the quiet redraw after that open's
 *  background check (keptReads) resolves it again against the fresh replies, and so does any later draw of its rota
 *  that finds it on another day or gone (F7: the fresh rota may arrive only with a live update), so a session moved or
 *  removed elsewhere is said there. */
let shiftLink:{id:string;serial:number;rota:string;day:string;person:string}|null=null;
function navigate(target:string){
  if(!leaving(()=>navigate(target),pageName(target)))return;
  const from=pageStep();setRoute(target);applyScheduleRoute();
  // D-075: a bare Schedule (New rota, Open rota: setWorkspace then navigate('schedule')) names the draft it shows at once,
  // not only when its draw ends (a reload meanwhile would open another rota).
  if(view==='schedule'&&!routeParts.length&&!routeQuery.size&&workspaceId)routeQuery.set('edit',workspaceId);
  const to=routeHash(view,routeParts,routeQuery);
  // The page already open, chosen again: the person asks for it afresh, so its kept replies do not answer (never a dead click).
  if(location.hash===to)forgetReads(new Set((paths[view]||paths.home).live));
  if(pageStep()!==from&&location.hash!==to)history.pushState({step:++shownStep},'',to);else history.replaceState(null,'',to);
  routeSeen=location.hash;return openPage();
}
/** A page opened by navigation (a link, the sidebar, Back) shows what this tab last read for it at once and checks it
 *  in the background (api.ts keptReads): a changed answer redraws it quietly, as a live update does (never under an
 *  open menu, dialog or field: the redraw is then owed); a failed check marks the page out of date. */
function openPage():Promise<void>{
  feedShown=null;
  const serial=renderSerial+1;
  return keptReads(renderPage,error=>{if(renderSerial!==serial||!me)return;
    if(error){showStale(true,error);return;}
    if(shiftLink?.serial===serial&&!routeShift)routeShift=shiftLink.id;  // re-resolved against the fresh replies
    if(readerBusy()){redrawOwed=true;return;}
    // Its reads were just checked: the redraw is drawn from them (a further change waits for the live update).
    void keptReads(renderPage,()=>{});});
}
// The Schedule's view, day, draft and layers live in the address (#schedule/<view or v.saved>/<day>?edit=<rota>&mode=&compare=),
// so a reload or a shared link opens the same thing; nothing about rotas is kept in browser storage.
let routeView='';
// The address the app last read or wrote. A different one means someone else changed it (a link, Back, a test) and its
// hashchange event is still to come: the app then never writes over it.
let routeSeen='';
function readRoute(fallback:string){
  setRoute(location.hash.length>1?location.hash:fallback);
  // An old bookmark shows the address it now opens (the Schedule writes its own, with its view, day and draft).
  const canonical=routeHash(view,routeParts,routeQuery);if(location.hash.length>1&&view!=='schedule'&&location.hash!==canonical)history.replaceState(null,'',canonical);routeSeen=location.hash;
  applyScheduleRoute();
}
/** A record link and a reloaded address apply the same schedule state. Bare Schedule keeps this visit's view. */
function applyScheduleRoute(){
  if(view!=='schedule'||schedSubPage()||!routeParts.length&&!routeQuery.size)return;
  let [a,b]=routeParts;const q=routeQuery;
  // An address naming no view says so instead of silently showing another (LIVE-35); old names keep working.
  const VIEW_ALIAS:Record<string,SchedView>={counts:'coverage',calendar:'week',grid:'week'};
  if(/^\d{4}-\d{2}-\d{2}$/.test(a||'')&&!b){b=a;a='';}
  if(a&&!a.startsWith('v.')&&!SCHED_VIEWS.some(v=>v.key===a)){
    if(VIEW_ALIAS[a])a=VIEW_ALIAS[a];
    else{routeNotice=`There is no “${a}” view. Showing ${SCHED_VIEWS.find(v=>v.key===schedView)?.label||'Week'}.`;a='';}}
  const dated=/^\d{4}-\d{2}-\d{2}$/.test(b||'');
  selected.clear();pickedDay='';plannerViewId='';routeView=a?.startsWith('v.')?a.slice(2):'';
  // A link must not inherit a search, saved-view restriction or "my duties" filter hiding the linked person/day.
  searchText='';schedSearch=undefined;schedFilters=q.get('person')?{person_ids:[q.get('person')!]}:{};schedConditions=null;mineOnly=false;gridColumns='day';gridCells='cards';gridRows='person';gridSubgroup='none';
  weekView.onlyOpen=false;weekView.onlyIssues=false;peopleView.find='';viewDays={...DAYS_DEFAULT};
  if(SCHED_VIEWS.some(v=>v.key===a)){schedView=a as SchedView;const r=q.get('range');viewRange=RANGE_NAMES.some(x=>x[0]===r)&&r!==VIEW_RANGE[schedView]?r as SchedRange:'';}
  planDayFromRoute=dated;if(dated)planDay=b;
  // SX-12: ?approve=<rota> opens that rota as ?edit= does, on its own days (not today's week).
  const approve=q.get('approve');if(approve)phoneReview=approve;
  const edit=q.get('edit')||approve;sourcePublication=q.get('source')||'';routeShift=q.get('shift')||'';  // ?shift= marks that session once it is drawn
  if(sourcePublication&&!dated)planDay='';  // An undated source link anchors once, when its publication is read.
  // A person/publication link without a draft names published duties; an explicit draft always wins.
  if(edit==='none'||!edit&&(!!onePerson()||!!sourcePublication)){workspaceId='';draftNone=true;}
  else{draftNone=false;if(edit)workspaceId=edit;}
  // A dated link without a draft chooses afresh, by the day it names (never the draft open before it).
  if(dated&&!edit)workspaceId='';
  jumpToDraft=!!edit&&edit!=='none'&&!dated;
  // Home's "Proposal ready · Review" (?review=proposal): Auto-schedule opens on that draft; a current result opens on Review.
  if(edit&&edit!=='none'&&q.get('review')==='proposal'){const s=schedulingByRota.get(edit)||{...freshScheduling(edit),...ownSetup(edit)};
    Object.assign(s,{open:true,step:3});schedulingByRota.set(edit,s);schedulingSettings=s;}
  const mode=q.get('mode');layerMode=mode==='side'||mode==='overlay'?mode:'edit';compareId=q.get('compare')||'';
  if(q.get('leave'))peopleView.leave=true;
}
function writeRoute(){
  const q=new URLSearchParams();
  if(workspaceId)q.set('edit',workspaceId);else if(draftNone)q.set('edit','none');
  // SX-02: on a phone the review keeps its ?approve= so a reload (or a phone waking the tab) opens it again.
  if(phoneReview&&workspaceId===phoneReview&&phoneLite())q.set('approve',phoneReview);
  if(routeQuery.get('panel')==='session'){
    const shift=routeQuery.get('edit')===workspaceId?routeQuery.get('shift'):null;
    if(shift){q.set('shift',shift);q.set('panel','session');}else{routeQuery.delete('shift');routeQuery.delete('panel');}}
  if(viewRange&&!plannerViewId)q.set('range',viewRange);if(layerMode!=='edit'&&workspaceId)q.set('mode',layerMode);if(layerMode==='side'&&compareId)q.set('compare',compareId);if(sourcePublication)q.set('source',sourcePublication);if(onePerson())q.set('person',onePerson());
  const query=q.toString(),route='#schedule/'+(plannerViewId?'v.'+encodeURIComponent(plannerViewId):schedView)+'/'+planDay+(query?'?'+query:'');
  // Never over a navigation already asked for (the hash changed, its event not yet handled): a late redraw finishing in
  // between would otherwise put the old view back, whichever page or view was asked for.
  if(view!=='schedule'||location.hash!==routeSeen||location.hash===route)return;
  history.replaceState(null,'',route);routeSeen=route;
}
// A refresh asked for while one runs (e.g. Undo right after a save) runs once more afterwards, so the newest server
// state always lands; callers awaiting it wait for that last run.
// A background refresh (the live pulse) reads the server state but does not redraw under someone using the page: if a
// menu, dialog or field opened while it read, it says so (refreshSkipped), the redraw is owed (redrawOwed, paid by the
// next draw of any kind) and the pulse waits for them instead.
let refreshForeground:string|null=null,refreshSkipped=false,redrawOwed=false;
/** The next render is a wait the person started (an explicit Refresh or Retry, a save's follow-up): it may show the top
 *  bar. A navigation (a new page or address) always may; a background redraw (a live update, polling) never does. */
let foregroundRender=false,renderedHash='';
const rulesSource={workspace:''};
// A live change names its scopes: the next refresh reads only what they can change. undefined: none owed; null: everything.
let refreshScopes:Set<LiveScope>|null|undefined;
// The changes behind refreshScopes (their ids, e.g. which rota), for kept answers per record (api.ts forgetReads).
let refreshChanges:LiveChange[]|null=[];
// One run at a time (loading.ts flight): a pulse or a save joins the run on its way; a Retry never stays dead behind one.
const refreshes=flight(stale=>refreshOnce(stale),()=>!!me);
function refresh(quiet=false,scopes?:Set<LiveScope>,changes?:LiveChange[],retry=false):Promise<void>{
  if(!me)return Promise.resolve();
  // A reload (another person's change, a pulse) may change the viewer's facts or the calendar: relative values ask again.
  forgetResolved();
  refreshScopes=scopes&&refreshScopes!==null?new Set([...refreshScopes||[],...scopes]):null;
  refreshChanges=scopes&&changes&&refreshChanges?[...refreshChanges,...changes]:null;
  if(!quiet)refreshForeground=pageStep();
  if(!retry)return refreshes.join();
  // A Retry reads the deferred lists afresh too (loadLists drops whichever answer is older).
  hydrating=null;return refreshes.retry();
}
/** What every Retry or Try again that reloads the app state calls, never plain refresh() (tests/test_web_loading.py). */
const retryRefresh=()=>refresh(false,undefined,undefined,true);
/** The lists the app keeps in memory (/api/catalogues). After the first load only the lists that changed since are
 *  sent (`known`: the versions held), so a save reloads what it changed, whatever its side effects. */
let catalogueReads=0,catalogueApplied=0,hydrating:Promise<void>|null=null;
const LIST_ROLES=[CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.CREDENTIALER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD];
const leaveTypesRead=(account:Me|null=me):Promise<{leave_types:Row[]}>=>(account&&(account.staff_swap_access||account.roles.some(r=>!hasAccess([r],CAPABILITY.STAFF))))
  ?background<{leave_types:Row[]}>('/api/leave-types'):Promise.resolve({leave_types:[]});
async function loadCatalogues(full=false,leave=leaveTypesRead()){
  // Every role picks a leave type (staff ask for leave); the full catalogue with archived types is the admin's list.
  await Promise.all([leave.then(r=>setLeaveTypes(r.leave_types),()=>{}),loadLists(full)]);
}
/** The deferred lists (memberships, policies, recurring series…) are held:
 *  boot reads only the lean set (`boot=true`); `_versions` names exactly the lists held, so an absent one is not loaded,
 *  never empty. */
const hydrated=()=>'policies' in ((catalogues._versions||{}) as object);
const needsDeferredCatalogues=()=>!['home','inbox','feedback'].includes(view)||routeParts.includes('related');
/** The one loader for the deferred lists: anything that reads them awaits this first. One read at a time (callers share
 *  it); the existing catalogue read with `known` sends just what is missing or changed. Staff hold no lists. */
function ensureCatalogues():Promise<void>{
  if(!me||!permits(...LIST_ROLES)||hydrated())return Promise.resolve();
  // A read overtaken by a newer one (a refresh, still lean) is dropped by loadLists: read again.
  return hydrating||=loadLists(true).finally(()=>{hydrating=null;}).then(applied=>applied?undefined:ensureCatalogues());
}
async function loadLists(full=false){
  // Every start, a staff one too, overtakes the reads begun before it; one begun for another account never applies.
  const mine=++catalogueReads,session=sessionKey();
  if(!permits(...LIST_ROLES)){catalogueApplied=mine;catalogues={people:[]};return true;}
  const held=catalogues._versions as unknown as Record<string,string>|undefined;
  // Until the deferred lists are held, a refresh stays lean too (only what changed in the lean set).
  const lean=!full&&!hydrated()?'boot=true':'';
  const reply=await background<Catalogues>('/api/catalogues?'+[held?'known='+Object.values(held).join(','):'',lean].filter(Boolean).join('&'));
  // A role whose people catalogue withholds contacts reads them from the authorised directory, joined by id: the one
  // place every People consumer (list, panel, redraw, Ctrl K) reads. Never staff numbers, e-mail or the editable title.
  if(reply.people?.some(p=>!('work_phone' in p))){
    // Unread contacts fail the whole reply (never an empty directory): its versions are not kept, so the next refresh
    // reads the people and their contacts again.
    const contacts=new Map((await background<{people:Row[]}>('/api/directory')).people.map(d=>[d.id,d]));
    reply.people=reply.people.map(p=>{const d=contacts.get(p.id);return d?{...p,work_phone:d.work_phone,work_extension:d.work_extension,pager:d.pager,directory_section:d.directory_section,contacts:d.contacts}:p;});
  }
  // Replies apply in the order the reads started: one that started earlier and lands later (a refresh overtaken by a
  // sign-in or role change) is dropped, never merged over fresher lists.
  if(mine<catalogueApplied||session!==sessionKey())return false;catalogueApplied=mine;
  catalogues=held&&catalogues._versions?{...catalogues,...reply}:reply;
  retiredRules=null;effectiveSources={};retiredVisit++;
  setTypeColours(catalogues.assignment_types||[]);setRankColours(catalogues.categories||[]);
  return true;
}
// What was saved since the last refresh: the nav badges reload only the counts those saves can change.
const savedKinds=new Set<string>();
window.addEventListener('rota-saved',e=>{const kind=(e as CustomEvent).detail?.kind;if(kind&&!READ_ONLY_COMMANDS.has(kind))savedKinds.add(kind);accessRead=null;});
// The live scopes the account (/api/me: settings, roles, duty-type colours, training) and the lists (/api/catalogues) carry.
const ME_SCOPES:LiveScope[]=['settings','access','catalogue','people'],LIST_SCOPES:LiveScope[]=['catalogue','people','rules','access'];
async function refreshOnce(stale=()=>false){
  forgetReads(refreshScopes??null,refreshChanges);refreshChanges=[];  // maintenance/118: kept answers (api.ts keptRead) last across redraws until a change can alter them
  if(!me)return;accessRead=null;readAfresh();const saved=[...savedKinds];savedKinds.clear();const local=inPlace&&saved.length>0;inPlace=true;
  const from=refreshForeground,quiet=from===null;refreshForeground=null;refreshSkipped=false;
  const scopes=refreshScopes??null;refreshScopes=undefined;const touches=(of:LiveScope[])=>!scopes||of.some(s=>scopes.has(s));
  // Only draft edits since the last refresh, patched in place, on the Schedule: it redraws reading just the draft (the
  // session, lists in memory and the other counts cannot have changed). Anything else reads what it can affect.
  if(local&&view==='schedule'&&plannerMemo)return redraw();
  // Read the account first: changed roles or an inactive staff binding must gate the catalogue requests too.
  try{const session=sessionKey(),account=touches(ME_SCOPES)?await background<Me>('/api/me'):me;
    if(session!==sessionKey()||stale())return;
    me=account;applyDisplay();
    if(touches(LIST_SCOPES))await loadCatalogues();
    if(session!==sessionKey()||stale())return;
    refreshFailed=false;showConnection(true);void loadNavBadges(true,saved.length?saved:scopes?[]:undefined,scopes);
    // A save can close its dialog before the follow-up reads finish. The page opened since then owns its draw.
    if(!quiet&&pageStep()!==from)return;
    if(quiet&&readerBusy()){redrawOwed=true;refreshSkipped=true;return;}
    // Drawn from here on, by either path: an owed redraw is paid (else every pulse would refresh again).
    redrawOwed=false;
    // A "Show their…" child list re-reads in place (keeps Show more; bulk_children.ts). A failed re-read shows its own
    // error and keeps the stale banner.
    const related=routeParts.indexOf('related'),inPlace=related>=0?await refreshRelated(routeParts[related+1]):undefined;
    if(inPlace!==undefined){if(inPlace)showStale(false);return;}
    foregroundRender=!quiet;await renderPage(quiet);}
  catch(e){if(sessionChanged(e)||stale())return;refreshFailed=true;showConnection(false,e);
    // A page showing data carries the stale banner, a page that did not load its own Retry; with nothing on screen, a
    // message says the refresh failed.
    if(!pageFailed&&!document.querySelector('#main>.stale-banner'))notify('The latest server state could not be retrieved. Previously confirmed saves remain saved. '+(e as Error).message,'error',undefined,e);}
}
/** Signing in and reloading take the same path: no splash. Once the account is known the app frame (sidebar, top bar
 *  with the person's name, footer) appears at once with the page as a skeleton; the sign-in card fades out over it. */
// While a start reads the account's lists and live baseline, a page asked for (a link, Back, a pulse) is not drawn: the
// start draws the page last asked for once, with its lists, instead of a draw it would then replace under the reader.
let bootAttempt=0,bootRead:AbortController|null=null,booting=false;
/** The session's token pair is rotated (/auth/refresh) when the app opens with a session last rotated over 12 hours ago,
 *  so a long remembered session does not keep one bearer token. The first open on a device only starts the clock.
 *  shortcut: a request another tab sends at the same instant with the old cookie is refused (it signs in again); add a
 *  short server-side grace for the previous token if that is ever seen. */
const ROTATED='rota.rotated',ROTATE_MS=12*3_600_000;
const rotatedNow=()=>{try{localStorage.setItem(ROTATED,String(Date.now()));}catch{/* not kept: no rotation */}};
async function rotateIfDue(){let last=0;try{last=Number(localStorage.getItem(ROTATED))||0;}catch{return;}
  // Stamped only once the server confirms the rotation: a refusal or a lost connection tries again at the next open.
  if(!last){rotatedNow();return;}if(Date.now()-last<ROTATE_MS)return;if(await refreshSession())rotatedNow();}
/** The deployment's branding, read once per page load and never waited for: sign-in and start-up draw at once with what
 *  is shown (the generated defaults until it answers; unread or unreadable, they stay). A saved change or its Undo
 *  applies its reply at once. */
const readBranding=()=>{
  // This public read still applies when boot clears the signed-out session.
  const stop=new AbortController(),timer=setTimeout(()=>stop.abort(),READ_TIMEOUT);
  void fetch('/api/branding',{credentials:'omit',cache:'no-store',signal:stop.signal})
    .then(r=>r.ok?r.json():null).then(v=>{if(v)void applyBranding(parseBranding(v));},()=>{})
    .finally(()=>clearTimeout(timer));
};
window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail;if(String(d?.kind).startsWith('branding.')&&d.result?.branding)void applyBranding(parseBranding(d.result.branding));});
async function boot(card?:HTMLElement|null){
  // Each start (Retry, a role change) supersedes the one before: it asks afresh instead of joining a read that may be
  // stuck, and a late answer to an older start is dropped. The older start's account read is aborted, so even its late
  // 401 cannot report the session as ended (api.ts drops an aborted read's answer before the session-expired event).
  const mine=++bootAttempt,stale=()=>mine!==bootAttempt;booting=true;readAfresh();bootRead?.abort();const own=bootRead=new AbortController();let framed=false;
  try{if(!card)await rotateIfDue();
    // SW3-C-P01: after /api/me confirms the viewer may read leave types, start that read beside the catalogues. An
    // inactive staff account gets no forbidden background request; the account response is the permission boundary.
    const account=await request<Me>('/api/me',{signal:own.signal});if(stale())return;me=account;const leave=leaveTypesRead(account);rememberAccount(me.identity_id,me.display_name,me.account?.username);applyDisplay();readRoute('home');catalogues={people:[]};
    if(stepOf()===null)history.replaceState({step:0},'');shownStep=stepOf()!;
    shell();framed=true;document.querySelector('#main')!.append(el('div',{},pageSkeleton()));if(card)fadeOut(card);
    // A page cached from an earlier release talks to a newer server: say so (About Rota shows the same).
    if(me.release&&me.release!==WEB_RELEASE)newVersion();
    // The live baseline is read before the page: a change after it moves the next pulse (one refresh), one before it is
    // in the page's own read. Without it the first pulse cannot tell and would redraw the page for nothing.
    // Pages that need deferred lists load them in one read (not the lean set and then the rest, maintenance):
    // the page and the sidebar counts then start their reads together.
    readAhead(true);rulesAhead=rulesListFirst()?readRulesAhead(ctx()):null;
    await Promise.all([loadCatalogues(needsDeferredCatalogues(),leave),background<Row>('/api/pulse').then(p=>{seenActivity=p.activity;noticeNewVersion(p.build);},()=>{})]);
    // Live updates from the pulse just read, not from the first 10 s fallback tick: a change committed meanwhile that only
    // the stream announces (a worker's check completing) was missed, the stream's baseline being taken after it.
    if(stale())return;connected=true;booting=false;if(!stream)listen();void loadNavBadges(true,undefined,undefined,ahead?.list);forgetPushStatus();
    // Opened from an alert (/n/{id}): the page address becomes the app's own, then the alert's item opens.
    const alert=/^\/n\//.test(location.pathname)?await alertNotification(new URL(location.href)):undefined;
    if(alert!==undefined)history.replaceState(history.state,'','/'+location.hash);
    await renderPage();if(alert!==undefined){if(alert)openNoteTarget(alert as Row);else navigate('inbox');}
    if(justUpdated())notify('Updated to the latest version.','success');  // the silent reload of maintenance landed
    void reconcile();}
  catch(e){if(stale())return;booting=false;if(sessionChanged(e))return;if(e instanceof ApiError&&[401,403].includes(e.status)){await showLogin(SIGNED_OUT[e.code]||'');}else startFailed(e,framed);}
}
/** Signed in with the frame drawn, a page whose lists did not load says only that, in the page with Retry (as a page
 *  that fails later does), never that the app can't be reached; the Schedule keeps the start-up screen's words. */
function startFailed(e:unknown,framed:boolean){
  if(!framed||view==='schedule'){bootFailure(e,()=>void boot(),true);return;}
  pageFailed=true;document.querySelector('#main')?.replaceChildren(el('div',{},readFailed(failedPageName(),e,()=>void boot())));
}
/** The one start-up failure screen (signed in or out, N-12): what happened, what staff do meanwhile, Retry, and Sign out
 *  for a signed-in account that may be the problem. */
function bootFailure(error:unknown,retry:()=>void,signedIn=false){
  const again=button('Retry',retry,'primary');
  root.replaceChildren(el('main',{id:'main',class:'boot boot-failed',tabindex:-1},bootEmblem(),el('h1',{},brandShort(),' can’t be reached'),
    note('This is not an empty rota. No schedule could be retrieved. Use the approved departmental fallback and contact support.','error',error),
    error instanceof ApiError&&(error.status>=500||error.code==='READ_TIMEOUT')?el('p',{class:'hint'},(error as Error).message):'',
    el('div',{class:'actions'},again,signedIn?button('Sign out',()=>void signOut(),'secondary'):'')));
  again.focus();
  // SW3-C-19: the connection coming back tries once by itself (Retry stays for a server that is still down).
  window.addEventListener('online',()=>{if(again.isConnected)retry();},{once:true});
}
let renderedView='',renderedTab='';
/** The page being opened, as a skeleton in its own layout: tables, cards (Home, Insights), the calendar (Schedule),
 *  sections (Configuration). One per page load: the shell redrawn around it keeps it (and its timing). */
const SKELETONS:Record<string,Shape>={home:'cards',insights:'cards',schedule:'calendar',configuration:'panel'};
/** The Schedule loads in its view's own shape (S-29): a table for List, Coverage and Counts, lanes for Day, Timeline and
 *  People, a calendar for Week, Month and Year. */
const scheduleSkeleton=(v:string):Shape=>['list','coverage','counts','issues'].includes(v)?'table':['day','timeline','people'].includes(v)?'timeline':'calendar';
function pageSkeleton(){
  // A page that is a table loads as one (Schedule › Issues, Insights › Workload: I-03); Programmes' board as a timeline.
  const shape=view==='schedule'?scheduleSkeleton(routeParts[0]||'week'):view==='insights'&&routeParts[0]==='workload'?'table'
    :view==='programmes'&&[undefined,'','board'].includes(routeParts[0])?'timeline':SKELETONS[view]||'table';
  const kept=document.querySelector<HTMLElement>('#main .loading.page-sk');
  if(kept?.dataset.view===view+shape)return kept;
  const sk=skeleton(shape,`Loading ${(paths[view]||paths.home).title}…`,{fill:true});sk.classList.add('page-sk');sk.dataset.view=view+shape;return sk;
}
/** The planner bar takes the top bar's title, or the bar an overlapping refresh already put there, so there is only ever
 *  one. Kept outside schedulePage on purpose: a closure there holding the bar it replaced chained every older render
 *  (and its whole grid) to the live page (perf audit SC-01). */
function swapPlannerBar(bar:HTMLElement,fallback:HTMLElement){
  const slot=document.querySelector('.topbar .department-title, .topbar .planner-bar');
  if(slot){keepFocus(slot,()=>slot.replaceWith(bar));bar.closest('.topbar')?.classList.add('has-planner');}else fallback.append(bar);
}
/** Swap part of the page that may hold keyboard focus (the Schedule's top bar and body, redrawn in place): a control
 *  that had focus (button, link, checkbox, radio, select, text field) hands it to the same control in what replaced it,
 *  by kind and name and, where the name repeats, its row (focus_key.ts sameControl), instead of dropping it to the page.
 *  A text field keeps its caret. Nothing focused there: focus is left alone. */
const FOCUS_CONTROLS='button,a[href],input:not([type=hidden]),select,textarea';
/** The stable ids a repeated row carries (a session in the grid, a table row, a rule, an issue). */
const ROW_KEYS=['data-shift','data-row','data-rule','data-issue'];
const controlKey=(n:HTMLElement):ControlKey=>({kind:n.getAttribute('role')||n.tagName.toLowerCase()+(n instanceof HTMLInputElement?':'+n.type:''),
  name:(n.getAttribute('aria-label')||(n as HTMLInputElement).labels?.[0]?.textContent||n.textContent||'').trim(),
  row:(r=>r?ROW_KEYS.map(k=>r.getAttribute(k)).find(Boolean)||'':'')(n.closest(ROW_KEYS.map(k=>`[${k}]`).join(',')))});
function keepFocus(old:Element,swap:()=>void){
  const a=document.activeElement as HTMLElement|null,had=a&&a.matches(FOCUS_CONTROLS)&&old.contains(a)?a:null;
  const key=had&&controlKey(had),caret=had instanceof HTMLInputElement||had instanceof HTMLTextAreaElement?(()=>{try{return had.selectionStart;}catch{return null;}})():null;
  swap();
  if(!had||had.isConnected||document.activeElement&&document.activeElement!==document.body)return;
  const found=[...document.querySelectorAll<HTMLElement>(FOCUS_CONTROLS)],next=found[sameControl(key!,found.map(controlKey))];
  if(!next)return;next.focus({preventScroll:true});
  if(caret!==null&&(next instanceof HTMLInputElement||next instanceof HTMLTextAreaElement))try{next.setSelectionRange(caret,caret);}catch{/* a type without a caret */}
}
/** What a page kept on screen while the next one loads may not be used: a Configuration section's pane (its nav stays
 *  live), else everything kept. */
const staleShown=(kept:HTMLElement)=>kept.querySelector<HTMLElement>('.config-pane')||kept;
/** `live`: a background refresh's redraw (a live update); it gives way to someone who began using the page while it read
 *  (at the swap, below), as refreshOnce does before it starts. */
async function renderPage(live=false){
  const leaving=leaveCheck;
  if(!me||booting)return;const renderStarted=performance.now();if(view!=='schedule')watchPanelJob('');windowOnScreen=false;if(view!=='access')accessRead=null;redrawOwed=false;const readsAtStart=readsDone();captureTableScroll(document.querySelector<HTMLElement>('#main'));const serial=++renderSerial;reuseFor=reusePlanner?serial:0;reusePlanner=false;const route=paths[view]||paths.home;leaveCheck=null;if(view!=='rules')revealRules=[];
  const changedPage=view!==renderedView,active=document.activeElement as HTMLElement|null,rulesRead=rulesAhead;rulesAhead=null;
  if(changedPage||view!=='rules'||routeParts.length)rulesSource.workspace='';
  // Owner, 6 Nov 2026: the top bar only for what the person is waiting for (a page or address being opened, their own
  // Refresh); a background redraw keeps the page on screen and usable and swaps it quietly.
  const waited=changedPage||location.hash!==renderedHash||foregroundRender;foregroundRender=false;openingPage(waited);
  const quiet=live&&!waited;
  // SW3-A-24: leaving the Schedule closes the auto-schedule panel; it no longer opens by itself on the next visit.
  if(changedPage&&renderedView==='schedule')schedulingSettings.open=schedulingSettings.editing=false;
  // D-139: a panel editing a rota (Add session) closes, or asks, once the page shows another rota or leaves the Schedule.
  dockOutlived(view==='schedule'?workspaceId:'');
  const composite=active?.closest<HTMLElement>('[role=tablist],[role=radiogroup]');
  const focus=composite&&active&&['tab','radio'].includes(active.getAttribute('role')||'')?{role:active.getAttribute('role'),label:active.getAttribute('aria-label')||active.textContent,group:composite.getAttribute('role'),index:[...root.querySelectorAll(`[role=${composite.getAttribute('role')}]`)].indexOf(composite)}:null;
  documentTitle(route.title);
  // A search box being typed in keeps its focus and caret when the page redraws around it (live search).
  const nameOf=(n:Element)=>(n.getAttribute('aria-label')||n.textContent||'').trim();
  // (Not on the Planner: its redraws keep the grid and manage focus themselves.)
  // No closure here may read `active` (or any element of the page being left): every hook this render creates shares one
  // scope, and the next page would keep the previous one alive through it (SH-01).
  const activeName=active?nameOf(active):'';
  const kept=view!=='schedule'&&!changedPage&&!focus&&active&&active!==document.body&&active.closest('#main')&&active.matches('button,a[href]')
    ?{tag:active.tagName.toLowerCase(),name:activeName,nth:[...document.querySelectorAll(`#main ${active.tagName.toLowerCase()}`)].filter(n=>nameOf(n)===activeName).indexOf(active)}:null;
  const typingIn=!changedPage&&active instanceof HTMLInputElement&&active.type==='search'&&active.getAttribute('aria-label')?{label:active.getAttribute('aria-label')!,at:active.selectionStart??active.value.length}:null;
  const navKey=!changedPage&&active?.closest('.config-nav')?active.textContent:null; // a section switch keeps focus on its button
  // The frame stays across pages (SH-07): focus left on its nav item or top bar after a page change moves to the page too.
  const idle=()=>{const a=document.activeElement;return !a||a===document.body||changedPage&&!!a.closest('.sidebar,.topbar');};
  const orient=()=>{if(!idle())return;if(changedPage){scrollTo(0,0);
    // Focus after the first paint: focusing now would force a layout of the whole new page before it is shown.
    requestAnimationFrame(()=>setTimeout(()=>{if(idle())main.focus({preventScroll:true});}));} // focus() alone scrolled main's top under the sticky top bar
    else if(typingIn){const box=[...main.querySelectorAll<HTMLInputElement>('input[type=search]')].find(n=>n.getAttribute('aria-label')===typingIn.label);if(box){box.focus();box.setSelectionRange(typingIn.at,typingIn.at);}}
    else if(navKey!==null)[...main.querySelectorAll<HTMLElement>('.config-nav button')].find(b=>b.textContent===navKey)?.focus();
    else if(focus){const group=root.querySelectorAll(`[role=${focus.group}]`)[focus.index];[...group?.querySelectorAll<HTMLElement>(`[role=${focus.role}]`)||[]].find(n=>(n.getAttribute('aria-label')||n.textContent)===focus.label)?.focus();}
    // Any other control (a row button, a toolbar button): the same control in the redrawn page gets focus back; when it
    // is gone (the row approved away) the page keeps focus instead of dropping it to the document.
    else if(kept){const same=[...main.querySelectorAll<HTMLElement>(kept.tag)].filter(n=>nameOf(n)===kept.name);(same[Math.min(kept.nth,same.length-1)]||main).focus({preventScroll:true});}};
  // Moving to another page clears messages about the old one; anything offering Undo stays until it expires.
  // A new page: the reads of the one left are aborted (api.ts leavePage), so it stops building.
  // Another tab of a merged page (#system/audit → #system/imports) is leaving too: the tab left stops building.
  const tab=PAGE_TABS[view]?routeParts[0]||tabsFor(view)[0]?.key||'':'';if(view===renderedView&&tab!==renderedTab)leavePage();renderedTab=tab;
  // SW3-D-19: a failed save belongs to the page it was made on: another page does not carry its "Not saved".
  if(view!==renderedView&&saveError){saveError='';saveUnconfirmed=false;paintSave();}
  // A page change closes any open popover, the frame's too; the first page after start-up leaves none (only the loading
  // skeleton), so a frame menu opened while it loads (the account menu) stays open.
  if(view!==renderedView){leavePage();if(document.querySelector('#main[data-loaded-view]'))closePopover();document.querySelectorAll('#toast-tray .toast:not(.with-action)').forEach(t=>t.remove());renderedView=view;window.scrollTo(0,0);}  // a new page opens at its top
  if(unknownLink){unknownLink=false;notify('That link doesn’t open a page here, so Home is shown.','info');}
  if(routeNotice){notify(routeNotice,'warning');routeNotice='';}
  // A planner redraw (view, filter, live update, save) keeps the shell and the current grid on screen until the new
  // one is ready: no "Loading…" flash, and the grid keeps its scroll position. Other pages rebuild the shell.
  const keep=view==='schedule'&&!schedSubPage()&&!changedPage&&!!document.querySelector('.topbar .planner-bar')&&!!document.querySelector('#main .planner-main');
  // Configuration: switching sections keeps the old section on screen (inert) until the new one is ready, instead of a
  // blank "Loading…". shortcut: Configuration only; pages that query their own DOM while loading would see the stale copy.
  // SW3-A-23: the Fix list too: a background refresh keeps the list on screen (inert) instead of the page skeleton.
  const previous=changedPage||!(view==='configuration'||view==='schedule'&&schedSubPage()||!waited)?[]:[...document.querySelector('#main')?.childNodes||[]];
  // A live update's redraw reads while that page stays usable, so someone may open a filter or a dialog on it meanwhile.
  // What the frame shows for it (title, actions, footer status) is kept, so the redraw can give way at the swap (below).
  const frameShown=quiet&&!keep&&previous.length?{title:document.querySelector('.topbar .page-title'),actions:[...document.querySelector('.topbar .page-actions')?.childNodes||[]],
    status:[...document.querySelector('.app-footer .page-status')?.childNodes||[]],leave:leaving}:null;
  // The same page already on screen: if this read fails at the server or the connection, it stays (with the stale banner).
  const shownBefore=!changedPage&&!keep&&document.querySelector('#main')?.getAttribute('data-loaded-view')===view&&document.querySelector('#main')?.getAttribute('data-loaded-tab')===(routeParts[0]||'')?[...document.querySelector('#main')!.childNodes]:[];
  const sk=keep||previous.length?null:pageSkeleton();
  // A redraw of the page on screen keeps its card while that page stays usable; the swap below brings it to the new copy.
  dragWorkspace=null;if(!keep){dropHoverCard();shell();}const main=document.querySelector<HTMLElement>('#main')!;
  main.classList.toggle('page-planner',view==='schedule'&&!schedSubPage());  // the Fix list is a list: the window scrolls it
  main.classList.toggle('page-contained',view==='rules');main.classList.toggle('page-wide',['schedule','people','configuration','insights'].includes(view));
  // SW3-C-12: a planning address (a draft to edit, the Fix list) is restricted for a reader who plans nothing, on any
  // device, as other pages they cannot open are (not "Best on a desktop", which suggests they could plan there).
  const planningLink=view==='schedule'&&((routeQuery.get('edit')||'none')!=='none'||schedSubPage(routeParts))&&!permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR,CAPABILITY.ADMIN);
  if(!allowed(view)||planningLink){main.append(empty('Restricted page','Your account does not have access to this page.',button('Go to Home',()=>navigate('home'),'secondary'),'lock'));main.setAttribute('data-loaded-view','restricted');orient();return;}
  if(phoneLite()&&!fitsPhone(view,routeParts,routeQuery)){main.append(desktopOnly());main.setAttribute('data-loaded-view','desktop-only');orient();return;}
  const body=el('div',{});
  // Another rota is loading: the previous rota's grid stays visible but cannot be used (a click there would act on the
  // rota being left, e.g. open a session panel the new rota then drops).
  if(keep&&plannerRota&&plannerRota.id!==workspaceId)main.querySelector('.planner-main')?.setAttribute('inert','');
  // maintenance: Configuration keeps its section nav usable while the next section loads (another pick replaces it): only
  // the section left is inert, never the nav (an inert nav ignored every pointer click until the read finished).
  // Moving the page detaches its scrollers, which resets their native positions: put them back at once (not only when
  // the rebuilt page mounts), so a background redraw does not show the list at its top meanwhile.
  if(keep)main.removeAttribute('data-loaded-view');else{main.append(body);if(previous.length){body.append(...previous);staleShown(body).inert=waited;restoreTableScroll(main);restoreRulesScroll(main);}else body.append(sk!);}
  // maintenance: a read meeting rules being rebuilt (api.ts projectionReady): the old content gives way to the page's skeleton.
  let waiting=sk;const updating=()=>{if(serial!==renderSerial||waiting||keep)return;waiting=pageSkeleton();body.inert=false;body.replaceChildren(waiting);};
  window.addEventListener('projection-updating',updating);
  // Pages that use deferred lists wait for them; Home and messages read their own bounded endpoints.
  try{if(needsDeferredCatalogues())await ensureCatalogues();
    const content=el('div',{});buildingPage(content);const renderer:Record<string,(target:HTMLElement)=>Promise<void>>={
    home:t=>{const phone=phoneLite();return homePage(ctx(),t,{openCatalogue:k=>setupOpeners().catalogue(k),auditWords,selfApproved:selfApprovedPanel,subscription:subscriptionPanel,phone,fixList:id=>openIssues(id),allowed:phone?id=>allowed(id)&&fitsPhone(id):allowed});},
    schedule:schedulePage,
    requests:t=>requestsPage(ctx(),t,{query:routeQuery,setQuery:q=>{routeQuery=q;history.replaceState(null,'',routeHash(view,routeParts,q));},
      showDay:(day,person)=>navigate(routeHash('schedule',['week',day],{person}).slice(1))}),
    people:peoplePage,
    programmes:programmesPage,
    insights:t=>pageTabs(t,{fairness:async t=>{await fairnessPage(ctx(),t,allowed('schedule')?{publication:id=>{publicationId=id;navigate('published');},focus:()=>plannerRota}:{});
      // Auditors and programme leads read only person-fact approvals in Requests: they read the wishes here.
      if(!permits(CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)&&permits(CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD))t.append(await wishesPanel(ctx()));},workload:workloadPage,exports:exportsPage}),
    rules:async t=>{
      // SW3-B-14: a catalogue rule's ?source= (Home › Rule change › Review) opens its rule page, where the proposal is,
      // without drawing the whole Rules list first (minutes under load) only to move away from it.
      const source=!routeParts.length&&!routeQuery.has('exception')&&ruleCatalogueOn()?find('policies',routeQuery.get('source')||''):undefined;
      const entry=source&&source.status!=='retired'?catalogueEntryOf(source.definition?.rule):undefined;
      if(entry){routeQuery.delete('source');return navigate(`rules/${encodeURIComponent(entry.id)}`);}
      if(ruleCatalogueOn())await ruleCataloguePage(ctx(),t,{parts:routeParts,openTag,navigate,setLeave:dirty=>{leaveCheck=dirty;},whichRule:canLookupRules()?id=>whichRuleDialog(id||''):undefined,exceptions:exceptionsPanel,openRule:p=>{void openRuleSource(p.id);},openLine:openRuleRow,inDraft:draftHealth,ahead:rulesRead,
        source:rulesSource,frame:()=>{if(t.isConnected){liftHeader(t);liftFooterStatus(t);closeOrphanPopover();}}});else await rulesPage(t);
      // Needs you › Rule exception links the record itself (home.py: ?source=<rule>&exception=<id>).
      if(routeQuery.has('exception'))exceptionsPanel(routeQuery.get('exception')!);
      else if(routeQuery.has('source'))await openRuleSource(routeQuery.get('source')!);
      else if(routeQuery.has('simplify'))simplifyPanel(ctx(),refresh);
      // Needs you › Rule exceptions (the server's link_target "#rules?exceptions=1") opens what waits.
      else if(routeQuery.has('exceptions'))exceptionsPanel();},
    configuration:configurationPage,
    // One /api/access read serves People & roles, Role history and both tab counts for the visit
    // (a save, the pulse or leaving the page reads it again).
    access:t=>{const data=permits(CAPABILITY.ADMIN,CAPABILITY.ACCESS_SYSTEM_ROLES,CAPABILITY.ACCESS_INSTALLATION_ADMIN)?accessRead||=get<Row>('/api/access').catch(e=>{accessRead=null;throw e;}):null;
      return pageTabs(t,{people:t=>accessPage(t,data!,'people'),routes:t=>routingPage(ctx(),t,{phone:phoneLite()}),history:t=>accessPage(t,data!,'history')},
        data?()=>data.then(a=>({people:a.identities.length,history:a.bindings.length})).catch(()=>({})):undefined);},
    system:t=>pageTabs(t,{imports:dataPage,bulk:async t=>bulkEditorPage(t,ctx()),operations:operationsPage,audit:auditPage,feedback:t=>feedbackPage(t,{mine:false,query:routeQuery,triage:may('feedback.triage'),github:may('feedback.export_github')})}),
    inbox:inboxPage,
    feedback:t=>feedbackPage(t,{mine:true,query:routeQuery,triage:false,github:false}),
  };
  // "Show their…" child lists: #<list>/related/<child> (bulk_children.ts).
  const related=routeParts.indexOf('related');
  const building=(related>=0&&routeParts[related+1]?(t:HTMLElement)=>relatedPage(t,routeParts[related+1]):renderer[view]||renderer.home)(content);
  await (waited?track(building):building).finally(()=>{openingPage(false);window.removeEventListener('projection-updating',updating);});
  // A skeleton that showed stays its 400 ms, then the page fades in; one that never showed gives way at once.
  const shown=serial===renderSerial&&await settle(waiting);
  // Someone began using the page while this live update read (refreshOnce's rule, checked again now): the page stays as
  // it is, with its popover or dialog open, and the update is owed (onPulse replays it once they are done).
  if(serial===renderSerial&&frameShown&&readerBusy()){if(frameShown.title)document.querySelector('.topbar .page-title')?.replaceWith(frameShown.title);
    document.querySelector('.topbar .page-actions')?.replaceChildren(...frameShown.actions);document.querySelector('.app-footer .page-status')?.replaceChildren(...frameShown.status);
    leaveCheck=frameShown.leave;main.setAttribute('data-loaded-view',view);main.setAttribute('data-loaded-tab',routeParts[0]||'');buildingPage(null);redrawOwed=refreshSkipped=true;return;}
  if(serial===renderSerial){pageFailed=false;renderedHash=location.hash;liftHeader(content);liftFooterStatus(content);
      // The jobs may have been read while this page was still being built (markProposal then saw the previous footer).
      const shownDraft=view==='schedule'&&!schedSubPage()?scheduleCache.drafts.get(workspaceId):undefined;if(shownDraft)markProposal(shownDraft);
      const rehover=keepHover();body.inert=false;body.replaceChildren(content);if(shown)reveal(content);if(keep){const scroll=plannerScroll();keepFocus(main,()=>main.replaceChildren(body));scroll();}
      buildingPage(null);sayCheck();sayCancel();  // likewise the check's job: answered while this footer was detached, its words and Cancel were missed
      void liveModule?.then(m=>m.pageShown());  // the new footer replaced the run's strip: back in now, not at its next tick
      closeOrphanPopover();requestAnimationFrame(rehover);trackStatusFooter(document.querySelector<HTMLElement>('.app-footer')!);phoneBanner(main);if(!windowOnScreen)main.setAttribute('data-loaded-view',view);if(readsDone()>readsAtStart)refreshFailed=false;
      // Perf instrumentation (as rk-decorate): a page's render, start to drawn; requests included when it made any.
      // `keep`: a redraw of the page on screen (a save, a live update), which the PC measures read (PC-STEPS).
      performance.measure?.(`render:${view}${keep?':redraw':''}`,{start:renderStarted});
      // The banner speaks for data this page showed before; a page just opened has none of its own to be stale (N-10).
      if(!changedPage||readsDone()>readsAtStart)showStale(stale&&readsDone()===readsAtStart);main.setAttribute('data-loaded-tab',routeParts[0]||'');orient();restoreTableScroll(main);restoreRulesScroll(main);trackWidths(main);markOpen(main);openCatalogueSource(content);
      if(view==='home')seeNotices(content);
      if(view==='schedule')drawCauseFocus(main);
      if(view==='schedule'&&!schedSubPage()&&fixPanelRota)void drawFixPanel();
      // A linked session is marked only after mounting; explicit detail links use the existing planner panel.
      if(view==='schedule'&&!schedSubPage()&&routeShift){const id=routeShift;requestAnimationFrame(()=>{
        if(serial===renderSerial&&routeShift===id){routeShift='';const at=scheduleCache.drafts.get(workspaceId)?.shifts.find(s=>String(s.id)===id);
          const person=routePerson||(shiftLink?.id===id?shiftLink.person:'');
          shiftLink={id,serial,rota:workspaceId,day:at?toLocal(at.start,zone()).slice(0,10):'',person};markShift(id,person);routePerson='';
          // Settled: the link leaves the address (replaced, never a new entry), so Back or a reload does not replay it.
          if(routeQuery.get('panel')!=='session'){routeQuery.delete('shift');writeRoute();}
          const s=routeQuery.get('panel')==='session'?scheduleCache.drafts.get(workspaceId)?.shifts.find(s=>String(s.id)===id):undefined;if(s)openSession(s);}
      });}}}
  catch(e){if(serial===renderSerial){
    buildingPage(null);  // a failed build never stays the page being built (alive()); a superseded one was replaced by its successor's
    const unreachable=e instanceof ApiError&&(e.status===0||e.status>=500)&&!sessionChanged(e);
    if(unreachable&&(keep&&!main.querySelector('.planner-main[inert]')||shownBefore.length)){if(!keep){main.replaceChildren(...shownBefore.filter(n=>!(n instanceof Element&&n.matches('.stale-banner'))));staleShown(main).inert=false;restoreTableScroll(main);restoreRulesScroll(main);}
      refreshFailed=true;main.setAttribute('data-loaded-view',view);showStale(true,e);orient();return;}
    // maintenance: this tab runs an older bundle than the server's: reload onto it once, or with something unsaved open
    // offer Reload to update (the update bar stays too). A connection failure is not the bundle's fault.
    const update=updateAfter(!(e instanceof ApiError&&e.status===0));
    if(update==='reload')return;
    pageFailed=true;if(keep)main.replaceChildren(body);body.inert=false;const failed=readFailed(failedPageName(),e,update==='ask'?undefined:retryRefresh);
    if(update==='ask'&&!refused(e))failed.firstChild!.after(' ',button('Reload to update',()=>{if(!reloadFor(serverBuild))reloadToUpdate();},'primary small'));
    body.replaceChildren(failed,
    // A rota that fails to load must not trap the planner: every rota is one step away.
    view==='schedule'&&workspaceId?el('div',{class:'actions'},button('Show the schedule without this rota',()=>{workspaceId='';draftNone=true;void renderPage();},'secondary')):'');orient();}}
}

/** DL-045: a page that didn't load is named by what the person chose, short: the open tab's label, else the nav label;
 *  the Schedule's own pages by one noun ("the Fix list", "the score check"). Never the H1. */
const failedPageName=()=>{
  if(view==='schedule')return routeParts[0]==='issues'?'the Fix list':routeParts[0]==='benchmark'?'the score check':'the schedule';
  const tab=shownTabs(view).find(t=>t.key===routeParts[0]);
  return tab?.label||(paths[view]?navLabel(view):'this page');
};
/** A page that is for a desktop, opened on a phone (phone scope): say so kindly, lead Home, and still let an urgent look through. */
/** The page's own name (its title; the Schedule's sub-pages by theirs): the phone notice says it. */
const shownPageName=()=>view==='schedule'?routeParts[0]==='issues'?'Fix list':routeParts[0]==='benchmark'?scoreCheckTitle():'Schedule planning':paths[view]?.title||'this page';
function desktopOnly(){
  return desktopNotice(shownPageName(),el('div',{class:'desktop-only-actions'},button('Back to Home',()=>navigate('home'),'primary'),
      // H-12: Continue keeps what this visit already read (rota list, draft, the running Auto-schedule): a redraw, not a reload.
      // The person waits for this draw: the top bar and the shared skeleton show (never the notice, or nothing, meanwhile).
      button('Continue anyway',()=>{continueOnPhone(view);foregroundRender=true;void redraw();},'ghost desktop-only-continue')));
}
/** A desktop page opened on a phone with "Continue anyway": one slim line says so until dismissed (this visit). */
function phoneBanner(main:HTMLElement){
  main.querySelector('.phone-banner')?.remove();
  const page=view;
  // Continued, the Schedule is the planner (for those who plan) even where it would fit a phone.
  const desktop=!fitsPhone(view,routeParts,routeQuery)||view==='schedule'&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR);
  if(!phoneScope()||!phoneQuery.matches||!continued.has(page)||!desktop||phoneBannerOff.has(page))return;
  const banner=el('div',{class:'phone-banner',role:'status'},icon('monitor'),el('span',{},'Not optimised for phones.'),
    iconButton('Dismiss','close',()=>{phoneBannerOff.add(page);banner.remove();main.focus({preventScroll:true});},{class:'bare small'}));
  main.prepend(banner);
}
const fitted=(row:HTMLElement)=>{fitRow(row);return row;};
/** A page's opening section header moves into the top bar: its buttons beside the title, its description into the ⓘ tip. */
function liftHeader(content:HTMLElement){
  const slot=document.querySelector('.topbar .page-actions');
  // The page's own help line joins the ⓘ (made for it when the page has no other help); a count or the title is no help.
  const tip=(text:string|null|undefined)=>{text=text?.trim();if(!text||/^[\d,]+\s+\S+$/.test(text)||text===paths[view]?.title)return;
    const info=document.querySelector<HTMLElement>('.topbar .page-info');
    if(info){if(!info.dataset.tip?.includes(text))info.dataset.tip=`${info.dataset.tip} ${text}`;}else document.querySelector('.topbar .page-title h1')?.after(pageInfo(text));};
  document.querySelector('.topbar [data-page-context]')?.remove();
  const head=content.firstElementChild;
  if(head?.classList.contains('section-head')){
    tip(head.querySelector('p')?.textContent);
    const context=head.querySelector<HTMLElement>('[data-page-context]');if(context)document.querySelector('.topbar .page-title')?.append(context);
    // Page actions sit on the right, the main (primary) action last, next to the bell.
    const actions=[...head.querySelectorAll<HTMLElement>('.actions>*')].sort((x,y)=>Number(x.classList.contains('primary'))-Number(y.classList.contains('primary')));
    slot?.replaceChildren(...actions);
    head.remove();
  }
}
/** An address naming a tab this page does not have, or one this account cannot open, says so once (LIVE-10, LIVE-25). */
function tabRefused(known:boolean,shown:string){
  notify(`${known?'You can’t open that tab':'That tab doesn’t exist'}${shown?`, so ${shown} is shown`:''}.`,'info');}
/** A merged page's tabs (PAGE_TABS, filtered by role). The open tab lives in the address (#system/audit), so reload, Back
 * and links keep it; switching redraws the page (tabs activate on Enter/Space). The tab's own header and footer status lift
 * into the top bar and footer as on any page. */
async function pageTabs(target:HTMLElement,build:Record<string,(t:HTMLElement)=>Promise<void>>,counts?:()=>Promise<Record<string,number>>){
  const list=shownTabs(view),page=view,open=list.find(t=>t.key===routeParts[0])||list[0];
  if(routeParts[0]&&routeParts[0]!==open.key)tabRefused(PAGE_TABS[page].some(t=>t.key===routeParts[0]),open.label);
  if(routeParts[0]!==open.key){routeParts=[open.key];history.replaceState(null,'',routeHash(view,routeParts,routeQuery));}
  const strip=el('div',{class:'rule-tabs page-tabs',role:'tablist','aria-label':paths[page].title},...list.map(t=>{
    const b=el('button',{type:'button',role:'tab',id:`page-tab-${t.key}`,'aria-selected':String(t===open),'aria-controls':'page-tab-panel'},t.label);
    b.addEventListener('click',()=>{if(t!==open)navigate(routeHash(page,[t.key]).slice(1));});return b;}));
  // One tab: no strip, so the panel is a plain region of the page (a tabpanel needs its tab).
  const panel=list.length>1?el('div',{role:'tabpanel',id:'page-tab-panel','aria-labelledby':`page-tab-${open.key}`}):el('div',{});
  // Counts (when the page has them cheaply) as in Rules: a number beside the name, which stays the tab's name.
  const [,n]=await Promise.all([build[open.key](panel),counts?.()]);liftHeader(panel);
  if(n)for(const b of strip.querySelectorAll<HTMLElement>('[role=tab]')){const k=b.id.replace('page-tab-','');if(n[k]!==undefined){b.setAttribute('aria-label',b.textContent||'');b.append(el('span',{class:'tab-count','aria-hidden':'true'},String(n[k])));}}
  // One tab: no tab strip (nothing to switch to); the page title says what it is.
  target.append(el('div',{class:'page-tabs-wrap'},list.length>1?strip:'',panel));
}
function findingsBlock(evaluation:Evaluation|null|undefined,fresh=true){
  if(!evaluation)return note('Check the rota to see open places and issues.','warning');
  // A visible section with one table row per finding (was a collapsed <details>): nothing important is hidden.
  const when=(f:Finding)=>f.start?spanLabel(f.start,f.end,zone()):'';
  const level=(f:Finding)=>f.severity==='error'?badge(LEVEL_WORDS.must,'err'):f.severity==='warning'?badge(LEVEL_WORDS.could,'warn'):badge('Note','info');
  const section=el('section',{class:'panel'},sectionHead(fresh?TERMS.rotaCheck:TERMS.needsCheck,`${evaluation.error_count} must fix · ${evaluation.warning_count} could improve`));
  if(!fresh)section.append(note('The rota or its rules have changed. Check the rota again before submitting it for review.','warning'));
  if(!evaluation.findings.length)section.append(note('No scheduling issues found.','success'));
  else section.append(table(['Level','Issue','Person','When'],evaluation.findings.slice(0,200).map(f=>[level(f),causeText(f)?el('span',{},f.message,el('br'),el('small',{class:'muted'},causeText(f))):f.message,f.person_id?name('people',f.person_id):'',when(f)])));
  if(evaluation.findings.length>200)section.append(note('Showing the first 200 issues. Narrow the planning scope to see the rest.','warning'));
  if(evaluation.quota_report)section.append(quotaPanel(evaluation.quota_report,fresh,'quotas-check'));
  return section;
}

// Drag one person onto another to exchange them (draft only). Uses the atomic, fully validated draft.swap.
let dragWorkspace:WorkspaceData|null=null;
// Auto-schedule proposal shown on the grid (not saved until applied through the reviewed flow).
let proposal:{job_id:string;workspace_id:string;additions:Row[];removals:Row[];job:Row;workspace_revision:number;session:number}|null=null;
function dnd(node:HTMLElement,p:Row,who:string){
  if(!dragWorkspace||!p.id)return node;
  // No native title: the hover card says "drag to exchange" (SI-06, one tooltip). The cursor says link, not move: the drop
  // exchanges two people (SI-25). A drag starts by closing the card (SI-24); valid targets light up in CSS (SI-22).
  // The listeners are the document's (below), not each name's: a department week draws thousands of names on every redraw.
  node.draggable=true;node.classList.add('draggable');node.dataset.assignment=String(p.id);
  return node;
}
const dragNode=(e:Event)=>(e.target instanceof Element?e.target.closest<HTMLElement>('.draggable[data-assignment]'):null);
// dragend goes to the dragged name itself (once): a redraw during the drag detaches it, and its end never reaches the document.
document.addEventListener('dragstart',e=>{const node=dragNode(e);if(!node)return;dropHoverCard();e.dataTransfer?.setData('application/x-rota-assignment',node.dataset.assignment!);e.dataTransfer!.effectAllowed='link';node.classList.add('dragging');document.body.classList.add('is-dragging');
  node.addEventListener('dragend',()=>{node.classList.remove('dragging');document.body.classList.remove('is-dragging');document.querySelectorAll('.drop-target').forEach(n=>n.classList.remove('drop-target'));},{once:true});});
document.addEventListener('dragover',e=>{const node=dragNode(e);if(node&&e.dataTransfer?.types.includes('application/x-rota-assignment')){e.preventDefault();e.dataTransfer.dropEffect='link';node.classList.add('drop-target');}});
document.addEventListener('dragleave',e=>dragNode(e)?.classList.remove('drop-target'));
document.addEventListener('drop',e=>{const node=dragNode(e);if(!node)return;e.preventDefault();node.classList.remove('drop-target');const first=e.dataTransfer?.getData('application/x-rota-assignment');if(first&&first!==node.dataset.assignment&&dragWorkspace)swapDialog(heldDraft(dragWorkspace),first,node.dataset.assignment!);});
let selectionUpdate:(()=>void)|null=null;
function updateSelectionBar(){selectionUpdate?.();}
// A saved planner view keeps the view, its filters and how the week grid is shown.
type PlannerSettings=PlannerViewSettings;
// The List's columns (shown, order, wrap, widths) are the view's top-level hidden, order, wrap and widths.
type PlannerSaved={planner:PlannerSettings;conditions?:Conditions|null}&Partial<TableColumns>;
let plannerViewId='';
weekView.onWidth=()=>scheduleSync?.();
// Views saved before ranges existed: "month" is the calendar layout over a month; people and list showed the whole rota
// ("rota"), which now opens on the layout's own range.
const MODE_RANGE:Record<string,SchedRange>={week:'week',month:'month',person:'2weeks',list:'month',coverage:'week',timeline:'month'};
const MODE_VIEW:Record<string,SchedView>={person:'people',coverage:'coverage',list:'list',timeline:'timeline'};
/** The kind of item a saved layout draws (its properties' defaults; the phone agenda is chosen on the phone). */
const savedSurface=(mode:string,range?:string|null):ItemSurface=>mode==='person'||mode==='timeline'?'chip':range==='day'?'bar':range==='month'?'pill':'card';
/** The kind of item the Schedule draws now (schedule_item.ts), or null where it draws counts, rows of a table or a year. */
function surfaceNow():ItemSurface|null{
  const agenda=phoneQuery.matches&&weekLayoutNow()==='agenda';
  if(schedView==='people')return 'chip';
  if(schedView==='timeline')return cellsNow()==='counts'?null:'chip';
  // Coverage's table lists a cell's sessions as session items (planner_week sessionItems) in its popover.
  if(schedView==='coverage')return useGrid()?null:'agenda';
  if(['week','month','day'].includes(schedView))return useGrid()?(cellsNow()==='counts'?null:'chip'):agenda?'agenda':schedView==='day'&&!phoneQuery.matches?'bar':schedView==='month'?'pill':'card';
  return null;
}
/** A saved view as compared and applied: older fields read as today's (compact → density), defaults filled in. */
const plannerPart=(p:Partial<PlannerSettings>|undefined)=>{const mode=p?.mode==='month'?'week':p?.mode||'week';
  return {mode,range:mode==='year'?null:(p?.range&&p.range!=='rota'?p.range:MODE_RANGE[p?.mode||'week']),search:p?.search||'',...(normalSearch(p?.search_options)?{search_options:normalSearch(p?.search_options)}:{}),location_id:null,assignment_type_id:null,person_id:null,filters:filtersOf(p),
    group_by:mode==='person'?(['rank','tag','team','none'].includes(p?.group_by||'')?p!.group_by:'rank'):p?.group_by||'service',people_sort:mode==='person'&&p?.people_sort&&p.people_sort!=='hours_desc'?p.people_sort:null,sort_by:p?.sort_by||'time',colour_by:p?.colour_by||'type',colour_tags:p?.colour_tags?.length?[...p.colour_tags]:null,density:p?.density||(p?.compact?'compact':'comfortable'),only_open:!!p?.only_open,only_issues:!!p?.only_issues,
    label:p?.label?{parts:p.label.parts,separator:p.label.separator||'dot',honorific:p.label.honorific||'dr'}:null,
    properties:p?.properties?.length&&JSON.stringify(propertiesOf(p.properties,savedSurface(mode,p?.range)))!==JSON.stringify(propertiesOf(null,savedSurface(mode,p?.range)))?propertiesOf(p.properties,savedSurface(mode,p?.range)):null,
    weekdays:p?.weekdays?.length&&p.weekdays.length<7?[...p.weekdays].sort():null,week_start:p?.week_start??null,time_window:p?.time_window||null,days:p?.range==='custom'?p.days||DAYS_DEFAULT.days:null,columns:p?.columns&&p.columns!=='day'?p.columns:null,cells:p?.cells==='counts'?'counts':null,
    rows:mode==='timeline'&&p?.rows&&!['person','none'].includes(p.rows)?p.rows:null,
    coverage:(c=>c&&(c.rows&&c.rows!=='type'||c.columns&&c.columns!=='day')?{...(c.rows&&c.rows!=='type'?{rows:c.rows}:{}),...(c.columns&&c.columns!=='day'?{columns:c.columns}:{})}:null)
      (p?.coverage||(mode==='coverage'?{rows:p?.rows,columns:p?.columns}:null)),subgroup:p?.subgroup&&p.subgroup!=='none'?p.subgroup:null,collapsed:p?.collapsed?.length?[...p.collapsed].sort():null,layers:savedLayers(p?.layers),
    // Display settings: absent reads as the default and is not stored.
    person_display:p?.person_display&&p.person_display!==PLANNER_PERSON_DISPLAY_DEFAULT&&pickedPeople(p.search_options||undefined,p.filters).length?p.person_display:null,
    group_sort:p?.group_sort?.length?p.group_sort.filter(([,d])=>d==='desc'):null,
    team_rows:p?.team_rows&&p.team_rows!==PLANNER_TEAM_ROWS_DEFAULT?p.team_rows:null,staffing:p?.staffing===false?false:null,
    widths:p?.widths&&Object.keys(p.widths).length?Object.fromEntries(PLANNER_WIDTH_KEYS.filter(k=>Number(p.widths![k])>0).map(k=>[k,Math.round(p.widths![k])])):null} as PlannerSettings;};
const plannerDefinition=():PlannerSaved=>({...(schedConditions?{conditions:schedConditions}:{}),...tableColumns('planner-list'),planner:distinctAxes(plannerPart({mode:schedView==='year'?'year':(({people:'person',coverage:'coverage',list:'list',timeline:'timeline'} as Record<string,PlannerSettings['mode']>)[schedView]||'week'),range:schedView==='year'?null:rangeNow() as PlannerSettings['range'],
  search:searchText,search_options:schedSearch,filters:schedFilters,
  group_by:schedView==='people'?peopleView.group:weekView.groupBy,people_sort:schedView==='people'?peopleView.sort.replace('-','_') as PlannerSettings['people_sort']:null,sort_by:weekView.sortBy,colour_by:weekView.colourBy,colour_tags:weekView.colourTags,density:weekView.compact?'compact':'comfortable',team_rows:weekView.teamRows,staffing:weekView.staffing?null:false,only_open:weekView.onlyOpen,only_issues:weekView.onlyIssues,label:viewNames,properties:weekView.props,
  weekdays:viewDays.weekdays,week_start:viewDays.week_start,time_window:viewDays.time_window,days:rangeNow()==='custom'?viewDays.days:null,
  columns:gridColumns==='day'?null:gridColumns,cells:cellsNow()==='counts'?'counts':null,rows:schedView==='timeline'?gridRows:null,coverage:{rows:coverRows,columns:coverColumns},subgroup:useGrid()||schedView==='week'&&isSessionGroup(gridSubgroup)?gridSubgroup:null,collapsed:[...collapsed],
  layers:{draft:draftsShown,leave:peopleView.leave,mine:mineOnly,mode:layerMode},person_display:personDisplay,group_sort:weekView.groupSort.filter((x):x is [Dim,'asc'|'desc']=>isDim(x[0])),widths:weekView.widths}))});
/** Rows, a category as columns, the group and Then by are four different dimensions (the server refuses a repeat):
 *  a group or Then by repeating the rows or columns is dropped (the grid leaves it out too). */
const distinctAxes=(p:PlannerSettings):PlannerSettings=>{const taken=[p.rows||(p.mode==='timeline'||p.mode==='person'?'person':''),p.columns];
  const group_by=taken.includes(p.group_by)?'none':p.group_by,subgroup=p.subgroup&&![...taken,group_by].includes(p.subgroup)?p.subgroup:null;
  // Each level's order names an active level only (the server refuses any other).
  const group_sort=(p.group_sort||[]).filter(([k])=>k===group_by||k===subgroup);
  return {...p,group_by,subgroup,group_sort:group_sort.length?group_sort:null} as PlannerSettings;};
const sameView=(a:PlannerSettings|undefined,b:PlannerSettings|undefined)=>JSON.stringify(plannerPart(a))===JSON.stringify(plannerPart(b));
/** Applies a saved view (none: the defaults). Opened from an address (`route`), what the address names wins over the
 *  view's layers: its rota or publication (edit, source), a person (never hidden by My duties), mode and leave. */
function applyPlannerView(p:Partial<PlannerSettings>|undefined,conditions?:Conditions|null,route?:URLSearchParams){
  const v=plannerPart(p);schedConditions=conditions||null;
  const l=savedLayers(v.layers)||LAYER_DEFAULTS,named=(k:string)=>!!route?.has(k);
  if(!named('leave'))peopleView.leave=l.leave;
  if(!named('person'))mineOnly=l.mine&&!!me?.person_id;
  if(!named('mode'))layerMode=l.mode;
  // Whether a draft shows, never which: off shows the published rota only; on lets the Schedule choose one it may open.
  if(!named('edit')&&!named('source')&&!named('person')){if(!l.draft){workspaceId='';draftNone=true;compareId='';}else if(!draftsShown&&!workspaceId)draftNone=false;}
  draftsShown=l.draft;
  if(v.mode==='year')switchView('year');else setLayoutRange(MODE_VIEW[v.mode||'']||'calendar',(RANGE_NAMES.some(r=>r[0]===v.range)?v.range:MODE_RANGE[v.mode||'week']) as SchedRange);
  planMode=v.mode||'week';searchText=v.search||'';schedSearch=normalSearch(v.search_options);schedFilters={...v.filters||{}};viewNames=v.label||null;
  viewDays={weekdays:v.weekdays||null,week_start:v.week_start??null,time_window:v.time_window||null,days:v.days||DAYS_DEFAULT.days};
  gridColumns=v.columns||'day';gridCells=v.cells==='counts'?'counts':'cards';gridRows=isDim(v.rows)?v.rows:'person';coverRows=isDim(v.coverage?.rows)?v.coverage!.rows as Dim:'type';coverColumns=(v.coverage?.columns||'day') as GridColumn;gridSubgroup=isDim(v.subgroup)?v.subgroup:'none';
  if(v.mode==='person'){peopleView.group=v.group_by as typeof peopleView.group;peopleView.sort=(v.people_sort||'hours_desc').replace('_','-') as typeof peopleView.sort;}
  // Collapsed groups are part of the view (SI-35).
  collapsed.clear();for(const g of v.collapsed||[])collapsed.add(g);
  Object.assign(weekView,{props:v.properties||null,groupBy:schedView!=='timeline'&&personDim(v.group_by||'')||v.mode==='person'?'service':v.group_by,sortBy:v.sort_by,colourBy:v.colour_by,colourTags:[...(v.colour_tags||[])],compact:v.density==='compact',teamRows:v.team_rows??PLANNER_TEAM_ROWS_DEFAULT,staffing:v.staffing!==false,onlyOpen:v.only_open,onlyIssues:v.only_issues,
    groupSort:[...(v.group_sort||[])],widths:{...(v.widths||{})}});
  personDisplay=v.person_display||PLANNER_PERSON_DISPLAY_DEFAULT;
  selected.clear();
}
// Drafts are re-checked automatically after any change (once per content version); no button needed.
let autoCheckKey='',autoChecking=false;
/** The rota checks this page just waited for (their lean status row with the complete-check delta, by job id): the
 *  Auto-schedule panel shows a finished check from it instead of reading the whole job again (142 KB, P0 capture). */
const checkRows=new Map<string,Row>();
const keepCheckRow=(job:Row|null|undefined)=>{if(!job?.id||!job.delta?.validation)return;checkRows.set(String(job.id),job);
  while(checkRows.size>8)checkRows.delete(checkRows.keys().next().value!);};
/** A finished check as the panel reads a job: its result is the check itself (a validation job's result is its validation). */
const heldCheck=(id:string)=>{const row=checkRows.get(id);return row?{...row,result:row.delta.validation}:null;};
/** Terminal validation jobs whose compact delta was just applied; their paired
 * workspace pulse must not start a second refresh. */
const checkSettled=new Map<string,number>();
/** The rota checks this page saw end (their last status row, by job id; the latest 16, in memory). A job list read before
 *  the end still lists such a check as queued: it is never joined again (autoCheck) nor offered Cancel, and every list the
 *  Auto-schedule panel draws, the kept one included, shows it as it ended (withEnded). */
const checksEnded=new Map<string,Row>();
const withEnded=(list:Row):Row=>!(list.jobs as Row[]|undefined)?.some(j=>checksEnded.has(String(j.id)))?list
  :{...list,jobs:(list.jobs as Row[]).map(j=>{const ended=checksEnded.get(String(j.id));return ended?{...j,...ended}:j;})};
const checkEndedSeen=({delta:_,...job}:Row)=>{checksEnded.set(String(job.id),job);  // its row without the check's delta (kept by checkRows)
  while(checksEnded.size>16)checksEnded.delete(checksEnded.keys().next().value!);
  if(lastJobs)lastJobs={...lastJobs,jobs:withEnded(lastJobs.jobs)};};
/** Cancel only for a check still going that has not been seen ending. */
const checkCancelShown=()=>checkCancellable(checkJob)&&!checksEnded.has(String(checkJob?.id));
// B7: when the running check started (the footer's clock), the check its Cancel set aside, and why a check stopped (the
// line above the grid). Either leaves the previous results shown, marked "Not checked".
// shortcut: Cancel stops waiting here; the server's check still finishes (a synchronous command). Make it a cancellable job with maintenance.
let checkStarted=0,checkCancelled='',checkStop:{rota:string;lead:string;cause:string;refused:boolean}|null=null;
/** A rota check runs as a validation job when a worker is serving jobs (maintenance): Cancel then stops it on the server
 *  (job.cancel, audited) and the saved results stay as they were. Without a worker the check is the synchronous
 *  workspace.validate, which cannot be cancelled, so no Cancel is offered. */
let checkJob:{id:string;key:string;status:string;since:number;w:Row}|null=null;
/** The check in hand is the synchronous one (no job): this page's own request is the check, so it is running. */
let checkSync=false;
/** The one automatic fallback (rule_check_api.ts checkTakeover). `checkTaken`: the jobs and rota versions already taken
 *  over, so it happens once and never loops; `checkOwn`: the take-over in hand; `checkGone`: the job it stopped watching;
 *  `checkUnstarted`: the rota version whose take-over failed too ("Check hasn't started · Check again"). */
const checkTaken=new Set<string>();
let checkOwn:{id:string;done:Promise<void>}|null=null,checkGone='',checkUnstarted='';
/** The joined job nobody is running: queued past the bound (a failed one has ended: `checkStop` says it). Not while
 *  this page is taking it over: the words stay the job's until it is stopped. */
const checkStalled=()=>!!checkJob&&checkOwn?.id!==checkJob.id&&checkUnfinished(checkJob.status,Date.now()-checkJob.since,jobWorkers.get(checkJob.id));
/** The check in words, from its job's own status; before job.create answers it waits, as a new job does. A job that
 *  ended while this page still waits for its results (checkingWords has no words for it) is still the rota check: never a
 *  blank line or a "Cancel: " without its operation (D-085). */
const checkSaid=()=>checkingWords(checkJob?checkJob.status:checkSync?'running':'queued')||checkingWords('running');
/** The job's status moved (queued → running): every check line drawn says it in place, without a redraw. */
const sayCheck=()=>{for(const line of document.querySelectorAll<HTMLElement>('[data-check-line]')){const said=checkSaid();
  if(line.dataset.checkLine!==said){line.dataset.checkLine=said;line.childNodes[1].textContent=said+' · ';line.querySelector('button')?.setAttribute('aria-label','Cancel: '+said.toLowerCase());}}
  for(const n of document.querySelectorAll<HTMLElement>('.dock-panel.fix-side .fix-gate .fix-checking'))if(n.lastChild)n.lastChild.textContent=checkSaid();
  // Submit, Approve and Publish held for this check say what its line says (schedulePage checkHold).
  for(const b of document.querySelectorAll<HTMLElement>('[data-check-hold]'))notYet(b,checkSaid());};
/** Someone is mid-task (a dialog, a popover, a field, a session panel): a redraw would rebuild it under them. */
const checkBusy=()=>!!document.querySelector('dialog[open],.popover,.dock-panel:not([data-rota-targets]):not(.fix-side)')||(!!document.activeElement?.matches('input,textarea,select')&&!document.activeElement.closest('[data-rota-targets]')&&!document.activeElement.matches('.wk-panel input[type=search]'))
  // A session panel is mid-task while it saves or a reason or confirm is open; idle (after its save), the check's result
  // draws beside it at once (its redraw keeps the panel and its focus), so the next edit never waits for it.
  ||!!document.querySelector('.wk-panel :is(.save-mark.saving,.wk-save-slot .spinner,form,.armed)');
/** The bound passed with the job still queued (at once when no worker answers): the page takes the check over, once; a
 *  job it may not take over again stops saying it waits (drawn once nobody is mid-task). */
const checkDue=(id:string)=>{const job=checkJob;if(job?.id!==id||!autoChecking)return;
  if(checkTakeover(job.status,Date.now()-job.since,checkTaken.has(id)||checkTaken.has(job.key),jobWorkers.get(id))){takeOver(job);return;}
  if(!checkStalled())return;if(checkBusy())setTimeout(()=>checkDue(id),1500);else void redraw();};
/** The job's wait is told now (as the live stream tells it) that its job was set aside, not at its next status read. */
const checkWake=(id:string)=>window.dispatchEvent(new CustomEvent('jobs-changed',{detail:[id]}));
/** The take-over: the queued job is set aside exactly as Cancel does (job.cancel, audited; its fixed reason is what
 *  Operations counts) and only then does this page run the check, the synchronous one. A failure is the wait's to say. */
function takeOver(job:NonNullable<typeof checkJob>){checkTaken.add(job.id);checkTaken.add(job.key);
  const done=(async()=>{
    // Stopped only if still queued: a check a worker has started by now is being served, and is followed.
    const set=await takeOverJob(job.id);checkSeen(set.job);
    if(!set.cancelled)return;
    checkGone=job.id;recordError(CHECK_TAKEN_OVER,'rota check');  // a feedback report sent from this page says it too
    if(checkJob?.id===job.id)checkJob=null;checkSync=true;sayCheck();checkWake(job.id);
    sayCancel();  // the server cannot stop this one
    await command('workspace.validate',{reason:'Automatic rota check',automatic:true},job.w.id,job.w.revision);
  })().catch(e=>{checkGone=job.id;checkWake(job.id);throw e;});
  done.catch(()=>{});  // read by the job's wait (waitCheck)
  checkOwn={id:job.id,done};}
/** The joined job's wait. Null: Cancel set it aside, or this page took it over and checked the rota itself. */
const waitCheck=async(job:Row,key:string)=>{const id=String(job.id),last=checkGone===id?null:
    await awaitJob(job,{seen:checkSeen,stop:()=>checkCancelled===key||checkGone===id});
  if(last&&DONE_JOB.includes(last.status))checkEndedSeen(last);
  // A status read can see this page's own take-over cancel before the cancel's reply: the take-over says what happened.
  if(checkGone!==id&&checkOwn?.id===id&&last?.status==='cancelled')await checkOwn.done.catch(()=>{});
  if(checkGone!==id)return last;
  // A whole-rota read can change the key while the same queued job still owns the failed take-over.
  if(checkOwn?.id===id)await checkOwn.done.catch(e=>{if(autoCheckKey===key)checkUnstarted=key;throw e;});
  return null;};
const stopJob=(id:string)=>command('job.cancel',{reason:'Cancelled the rota check'},id).catch(e=>{if(!sessionChanged(e))reportError(e);});
const cancelCheck=()=>{checkCancelled=autoCheckKey;autoChecking=false;if(checkJob?.key===autoCheckKey)void stopJob(checkJob.id);void redraw();};
const DONE_JOB=['succeeded','failed','cancelled','applied','discarded'];
/** The footer's "Checking the rota · m:ss" (B7, D76), alone on a first check and beside the kept counts on a re-check.
 *  Its Cancel is there only while the check is a job the server can stop (rule_check_api.ts checkCancellable): hidden
 *  until the check's job is known (job.create answered, or a check found running joined), never for the synchronous
 *  check run without workers, which the server cannot stop (UX-FIX-A). */
const checkLine=()=>{const said=checkSaid(),line=elapsed(said,checkStarted,cancelCheck),b=line.querySelector('button');line.dataset.checkLine=said;
  if(b){b.dataset.checkCancel='';b.hidden=!checkCancelShown();}return line;};
/** Every check line drawn shows its Cancel only while the check can be stopped (checkCancellable). */
const sayCancel=()=>{for(const b of document.querySelectorAll<HTMLElement>('[data-check-cancel]'))b.hidden=!checkCancelShown();};
/** The check is now a job the server can stop: every check line drawn before it shows its Cancel. */
const checkStoppable=(job:Row,key:string,w:Row)=>{const id=String(job.id),since=Date.parse(job.created_at)||Date.now();checkJob={id,key,status:String(job.status),since,w};
  sayCancel();sayCheck();
  if(job.status==='queued')readWorkers(id,w,()=>setTimeout(()=>checkDue(id),Math.max(0,since+checkBoundMs(jobWorkers.get(id))-Date.now())));};
/** The job workers as the rota's summary said them when a queued check job was first seen (by job id; null while the
 *  read is out or if it failed: no bound then, so no take-over, and the check says it waits). Bounded like checkRows. */
const jobWorkers=new Map<string,CheckWorker|null>(),workerReads=new Map<string,Promise<void>>();
const readWorkers=(id:string,w:Row,then:()=>void)=>{let read=workerReads.get(id);
  if(!read){jobWorkers.set(id,null);
    read=get<CheckWorker>('/api/workspaces/'+encodeURIComponent(String(w.id))+'/summary').then(s=>{
      jobWorkers.set(id,{worker_liveness:s.worker_liveness,worker_heartbeat_max_age_seconds:s.worker_heartbeat_max_age_seconds});});
    workerReads.set(id,read);
    while(workerReads.size>8){const old=workerReads.keys().next().value!;workerReads.delete(old);jobWorkers.delete(old);}}
  void read.then(then,()=>{});};
/** Each status read of the joined job: its words follow it. */
const checkSeen=(j:Row)=>{if(checkJob?.id!==j.id||checkJob.status===j.status)return;
  if(j.status==='failed'&&j.error_code==='STALE_INPUTS')return;  // maybe replaced by a newer edit's check: checkEnded says
  const was=checkStalled();checkJob.status=String(j.status);
  if(was)void redraw();else sayCheck();};  // picked up after all: the pill gives way to the running check
/** One rota check: a validation job (waited for, read each second) when workers serve jobs, else synchronous.
 *  shortcut: a worker lost after its probe leaves the job queued (Cancel still stops it). */
/** A check JOB that failed: the line above the grid says the job's own words (rule_check_api.ts checkStoppedWords). */
class CheckJobStopped extends ApiError{constructor(job:Row,details?:unknown){super(String(job.error_code||'CHECK_FAILED'),checkStoppedWords(job.error_code),409,details);}}
async function runCheck(w:Row,key:string,existing?:Row):Promise<Row|null>{
  await probeWorkers();
  if(existing){checkStoppable(existing,key,w);const last=await waitCheck(existing,key);if(checkJob?.id===existing.id)checkJob=null;
    return last&&checkEnded(last,w,key);}
  // maintenance: a provisional check (scope command_changes) means the server already queued the complete check for a live
  // worker; join that job (job.create coalesces) instead of a second, synchronous whole-rota check.
  if(!workersKnown()&&!serverChecking(w.validation)){checkSync=true;sayCheck();await command('workspace.validate',{reason:'Automatic rota check',automatic:true},w.id,w.revision);return null;}
  checkSync=false;sayCheck();
  // Superseded (the rota changed again before it started): the newer version's own check runs instead.
  const job=(await command<{job:Row|null}>('job.create',{workspace_id:w.id,kind:'validation',automatic:true},undefined,w.revision)
    .catch(e=>{if(e instanceof ApiError&&e.code==='STALE_REVISION')return {job:null};throw e;})).job;
  if(!job)return null;
  checkStoppable(job,key,w);if(checkCancelled===key){void stopJob(job.id);return null;}
  const last=await waitCheck(job,key);if(!last)return null;if(checkJob?.id===job.id)checkJob=null;
  return checkEnded(last,w,key);
}
/** The joined job ended. Set aside by someone else (their Cancel, or a take-over from another page on this rota): the
 *  rota's check is read first, and only one that is not current stopped (rule_check_api.ts checkEndedStopped, DL-024).
 *  A stale or failed job names why, as a refused synchronous check does (maintenance cause via its input changes). */
async function checkEnded(last:Row,w:Row,key:string):Promise<Row|null>{
  if(checkCancelled===key)return last;  // this person's own Cancel: "Not checked" (autoCheck)
  // Failed as stale because the rota was edited again (queue_validation): the newer edit's check counts, so this one is
  // dropped like a check superseded before it started, never drawn as stopped, also before the newer check is on the page.
  if(last.error_code==='STALE_INPUTS'&&await rotaMeta(String(w.id)).then(d=>d.workspace.revision>w.revision,()=>false))return null;
  const current=last.status==='cancelled'&&await rotaCheckCurrent(String(w.id));
  if(!checkEndedStopped(last.status,current))return last;
  const details=last.error_code==='STALE_INPUTS'?await get<Row>(`/api/jobs/${last.id}/input-changes?offset=0&limit=1`).then(r=>({changed_facts:r.changes}),()=>undefined):undefined;
  throw new CheckJobStopped(last,details);
}
const checkAgain=()=>{checkStop=null;checkCancelled='';checkUnstarted='';autoCheckKey='';void refresh();};
/** `existing`: a check already queued or running (Check the whole rota, another tab, a reload): D76 makes it this same
 *  footer item, with its elapsed time and Cancel, instead of the Auto-schedule panel's. */
function autoCheck(data:WorkspaceData,existing?:Row){
  const w=data.workspace,key=w.id+':'+data.current_fingerprint;
  if(existing&&checksEnded.has(String(existing.id)))return;  // a list read before it ended: it is over, not joined again
  // A job the server already has is new proof about the workers; without one the remembered probe stands.
  if(existing)void reprobeWorkers(String(existing.id));else probeWorkers();
  if(existing?(autoChecking||checkJob?.id===existing.id):autoCheckKey===key)return;
  autoCheckKey=key;autoChecking=true;checkStarted=existing?Date.parse(existing.started_at||existing.created_at)||Date.now():Date.now();checkStop=null;checkCancelled='';checkUnstarted='';
  checkSync=!existing&&!workersKnown()&&!serverChecking(w.validation);  // runCheck's own choice, known before the footer draws
  // Show the result without disturbing someone mid-task: wait while a dialog is open or a field has focus.
  const settle=(job?:Row|null)=>{if(view!=='schedule'||workspaceId!==w.id)return;
    // The result is taken in as it lands (its job is this page's own from now: onPulse skips its pulse); only the redraw
    // waits for someone mid-task.
    // A complete-check delta replaces the held validation in place. This is the
    // same authority boundary as an edit delta; missing/stale/partial deltas
    // fall back to the authoritative workspace read.
    const patched=job&&applyCheckDelta(scheduleCache.drafts.get(w.id),job);
    if(job?.id)checkSettled.set(String(job.id),Date.now());
    // The check changed only this rota's results: read the rota and its row again (a refresh after a job command would
    // redraw them from memory, leaving "Needs a check" up), never the account and lists.
    if(patched)scheduleCache.drafts.set(w.id,patched);else{readAfresh();forgetSchedule();}  // never join a read begun before the check landed
    // A draw begun since it landed (the person's own view change) read the checked rota and shows it: no second one.
    const landed=renderSerial;
    const show=()=>{if(view!=='schedule'||workspaceId!==w.id||renderSerial!==landed&&!redrawOwed)return;
      // Working in a session panel counts too: a redraw would rebuild it under the person.
      // The rota's Workload targets dock is not busy: its refresh redraws only the targets. Nor is the Fix list panel: it is
      // drawn again in place with the checked counts, keeping the focus where it was (drawFixPanel), so the footer, the
      // grid and the panel never wait on it (QA cycle 3 maintenance).
      // ME-21: focus left on the Auto-schedule panel (its Apply) is not work in progress: the panel survives a redraw (its
      // step is kept), so the grid and footer show the checked rota at once instead of when focus moves.
      if(checkBusy()){setTimeout(show,1500);return;}
      void redraw();};
    show();void loadNavBadges(true,['workspace.validate']);};
  // A refused check is said once for this version of the rota (SR-19), not left as a silent "Needs a check".
  // A cancelled check's late answer is left alone: the person chose to go on with the previous results.
  if(existing){checkStoppable(existing,key,w);void redraw();}  // the footer shows it now, not at the next draw
  // Superseded by a newer edit's check on this rota (autoCheckKey moved on): this one's result is dropped, never drawn,
  // and the checking state stays the newer check's.
  const superseded=()=>autoCheckKey!==key&&autoCheckKey.startsWith(w.id+':');
  void runCheck(w,key,existing).then(job=>{if(checkCancelled===key||superseded())return;autoChecking=false;keepCheckRow(job);settle(job);}).catch(e=>{if(checkCancelled===key||superseded())return;autoChecking=false;
    // The page's own take-over failed too: the check hasn't started (the footer pill says so), with why.
    if(checkUnstarted===key){if(!sessionChanged(e))reportError(e);}
    // It names what stopped it (maintenance's cause) above the grid with Check again, never a toast that goes away (B7).
    // A failed job with no named cause is its own words alone (checkStoppedWords); a refused
    // synchronous check keeps the server's words after the elapsed time.
    else if(!sessionChanged(e)){const err=e as ApiError,s=stoppedWords(Date.now()-checkStarted,err.details),job=e instanceof CheckJobStopped;
      checkStop={rota:w.id,lead:job&&!s.cause?err.message:s.lead,cause:s.cause||(job?'':err.message),refused:checkRefused(err.code,err.status)};}settle();});
}
// A double-booked session cancelled elsewhere (another draft, a recurring session) leaves the open draft's check out of
// date: check it again by itself, once for cancels in quick succession. If that fails, the draft's "Check rota" stays.
let recheckWait:Promise<void>|null=null;
setClashRecheck(()=>recheckWait||=new Promise<void>(done=>window.setTimeout(async()=>{recheckWait=null;
  const id=new URLSearchParams(location.hash.split('?')[1]||'').get('draft')||workspaceId;
  try{if(id){const d=await rotaMeta(id),w=d.workspace;
    if(w.status==='draft'&&!w.archived_at&&permits(CAPABILITY.COORDINATOR)&&w.validation?.fingerprint!==d.current_fingerprint)
      await command('workspace.validate',{reason:'Check again after cancelling a double-booked session'},w.id,w.revision);}}
  catch(e){if(!sessionChanged(e))notify(`The rota could not be checked again: ${(e as Error).message} Use Check rota.`,'warning');}
  finally{done();}},400)));
/** Department rules are every rota's defaults; this shows the ones in force for one rota and lets a draft change them for this rota only. */
/** Workload targets for this rota (a dock beside the Schedule): the latest check's targets, this rota's saved target
 *  adjustments and Adjust workload target. Its rules are the rules panel's (planner_rules.ts, the server's rule map:
 *  one list, the same reach the checks use). `again`: a planner redraw refreshes an open dock in place. */
function rotaTargetsDialog(rotaId:string,again=false){
  const current=document.querySelector<HTMLElement>('[data-rota-targets]');
  if(again&&current?.dataset.rotaTargets!==rotaId)return;
  const targets=plannerTargets?.()||null;
  dockPanel({key:'rota-targets',title:ROTA_TARGETS,width:620,refresh:again,
    build:({panel,body})=>{panel.dataset.rotaTargets=rotaId;
      body.append(targets||empty('No workload targets','This rota’s check found no workload targets to adjust.',null,'sliders'));}});
}
/** This rota's rules, from anywhere (the Fix list, Score check, a finding's action): the schedule opens first. */
function openRotaPanel(rotaId:string,ruleId=''){
  if(openRotaRulesFor(rotaId,ruleId))return;
  workspaceId=rotaId;draftNone=false;void navigate(routeHash('schedule',[schedView,planDay],{edit:rotaId}).slice(1));
}
type BaselineRefreshPreview={workspace_id:string;revision:number;baseline:{snapshot_hash:string;included_sources?:string[];omitted_sources?:string[]};current_hash:string;changes:{source:string;status:string;snapshot_hash:string;department_hash:string;draft_hash:string;safety?:boolean;mandatory?:boolean}[];conflicts:string[];ready:boolean;captured_initial?:boolean;local_adjustments?:Row[];target_overrides?:Row[]};
const baselineDriftCache=new Map<string,Pick<BaselineRefreshPreview,'changes'>>();
/** A draft's baseline against current department changes: the one call for the bar's count and the review dialog. */
// A read (maintenance's GET, 93bd145ed): it never takes the write path.
const baselinePreview=(w:Row)=>background<BaselineRefreshPreview>(`/api/workspaces/${encodeURIComponent(String(w.id))}/baseline/refresh-preview`);
/** The planner bar's drift control: one maker for the bar and for an answer that lands after the bar was drawn. */
/** "N department changes": the one wording for the planner bar and the Rules page. */
const departmentChanges=(n:number)=>plural(n,'department change');
const driftName=(n:number)=>`${departmentChanges(n)} since this draft was created`;
// Visible text, never icon-only: it changes what the draft is built on. A tight bar keeps icon + count (the count pills' step).
const baselineDriftButton=(w:Row,changes:number)=>{
  const b=el('button',{type:'button',class:'health-pill baseline-drift','aria-label':driftName(changes)},
    icon('history'),el('span',{class:'num'},String(changes)),el('span',{class:'label'},`department change${changes===1?'':'s'}`));
  b.addEventListener('click',()=>void refreshDraftBaseline(w,()=>refresh()).catch(reportError));return b;};
/** D-053: a failed drift check says so with Retry (never nothing on the bar, which reads as "no department changes"). */
const baselineDriftFailed=(e:unknown,retry:()=>void)=>{const n=failedNote('Couldn’t check department changes.',e,retry);
  n.classList.add('baseline-drift');n.querySelector('button')?.setAttribute('aria-label','Retry checking department changes');return n;};
function requestBaselineDrift(w:Row,landed?:()=>void,failed?:(n:HTMLElement)=>void){
  if(!w.baseline)return;
  const key=baselineDriftKey(w);if(baselineDriftCache.has(key))return;
  baselineDriftCache.set(key,{changes:[]});
  void baselinePreview(w).then(preview=>{
    // Only department changes are offered: a draft's own edits need no refresh (refreshPlan says the same).
    const changes=(preview.changes||[]).filter(c=>c.status!=='draft');baselineDriftCache.set(key,{changes});
    // The control joins the bar already drawn, after the rota's status. Never a page refresh: that read everything
    // again on every opening and redrew the page under what the person was doing (a run just started lost its answer).
    const bar=driftBar(w,key);bar?.querySelector('.baseline-drift.read-failed')?.remove();  // a Retry that answered
    if(changes.length&&bar&&!bar.querySelector('.baseline-drift'))
      (bar.querySelector(':scope>.rota-status')||bar.firstElementChild)?.after(baselineDriftButton(w,changes.length));
    if(changes.length)landed?.();  // a caller without the planner bar (a rule's draft health) draws its own control
  }).catch(e=>{
    if(sessionChanged(e))return;const retry=()=>requestBaselineDrift(w,landed,failed),bar=driftBar(w,key);
    if(bar){bar.querySelector('.baseline-drift')?.remove();(bar.querySelector(':scope>.rota-status')||bar.firstElementChild)?.after(baselineDriftFailed(e,retry));}
    failed?.(baselineDriftFailed(e,retry));
  });
}
/** The planner bar showing this draft at this baseline, if any. */
const driftBar=(w:Row,key:string)=>{const bar=document.querySelector('.planner-bar.review-bar:not(.fix-bar)');
  return bar&&plannerRota?.id===w.id&&baselineDriftKey(plannerRota)===key?bar:null;};
function baselineDriftFor(w:Row){return baselineDriftCache.get(baselineDriftKey(w));}
/** Review the department changes since a draft was created, then adopt them into its baseline. */
async function refreshDraftBaseline(w:Row,refreshPage:()=>Promise<unknown>){
  const preview=await baselinePreview(w);
  baselineDriftCache.set(baselineDriftKey(w),{changes:(preview.changes||[]).filter(c=>c.status!=='draft')});
  const changed=preview.changes||[],plan=refreshPlan(changed);
  // No department change: said in one line, with nothing to apply (no reason asked, no audit entry for an empty refresh).
  if(!plan.apply){showDialog('Refresh draft baseline','',body=>body.append(note(plan.empty,'info')));return;}
  formDialog('Refresh draft baseline','',form=>{
    const decisions=new Map<string,HTMLSelectElement>(),fixed:Record<string,string>={};
    // L11: Apply waits, saying why, until every conflict is decided ("Choose…" until then).
    const why=el('span',{class:'decide-count',role:'status',id:`refresh-why-${w.id}`});
    const sync=()=>{const b=form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');
      const open=[...decisions.values()].some(s=>!s.value);why.textContent=open?UNDECIDED:'';
      if(!why.isConnected)form.querySelector('.form-footer')?.prepend(why);
      if(b){b.disabled=open;if(open)b.setAttribute('aria-describedby',why.id);else b.removeAttribute('aria-describedby');}};
    // A mandatory safety rule always follows the department: its one outcome is said, not offered.
    const decide=(item:BaselineRefreshPreview['changes'][number])=>{const name=baselineSourceLabel(item.source);
      if(item.safety){fixed[item.source]='use_department';return SAFETY_DECIDED;}
      const s=select(`Decision for ${name}`,[{value:'use_department',label:'Use department'},{value:'keep_draft',label:'Keep this draft'}]);
      s.input.setAttribute('aria-label',`Decision for ${name}`);s.input.addEventListener('change',sync);decisions.set(item.source,s.input);return s.input;};
    const rows=changed.map(item=>[baselineSourceLabel(item.source),changeLabel(item),
      ...(plan.decide?[item.status==='conflict'?decide(item):'']:[])] as (string|HTMLElement)[]);
    const list=table(['Group','Change',...(plan.decide?['Decision']:[])],rows);list.classList.add('span-2');
    const reason=area('Reason','');reason.input.required=true;reason.input.maxLength=500;
    form.append(list,reason.wrap);
    if(decisions.size)queueMicrotask(sync);  // the footer and its Apply are added after this builder returns
    return async()=>{
      await command('workspace.baseline.refresh.apply',{expected_baseline_hash:preview.baseline.snapshot_hash,decisions:{...fixed,...Object.fromEntries([...decisions].map(([k,v])=>[k,v.value]))},reason:reason.input.value},w.id,w.revision);
      await refreshPage();notify('Draft baseline refreshed.','success');
    };
  },'Apply refresh');
}
async function captureDraftBaseline(w:Row,refreshPage:()=>Promise<unknown>){
  await command('workspace.baseline.capture',{reason:'Capture the draft baseline before auto-schedule'},w.id,w.revision);
  baselineDriftCache.delete(baselineDriftKey(w));
  await refreshPage();
  notify('Draft baseline captured. Auto-schedule is ready to review.','success');
}
/** The one Schedule page (replaces Planner, Rota calendar, Team calendar and Published rota): a continuous timeline in
 *  seven views over the published layer, one editable draft (or two side by side) and leave & wishes. */
/** Why the page chose the rota it opened on its own (SR-03), said in the rota switcher; '' once someone picks one. */
let draftWhy='';
async function schedulePage(target:HTMLElement){const mySerial=renderSerial,viewAtStart=plannerViewId;
  if(routeParts[0]==='issues'){const id=routeQuery.get('draft')||workspaceId;
    // The Fix list (D9) by default; All checks (its other saved view) is the per-check table, resolved in batches.
    if(permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR))return routeQuery.get('view')==='checks'?issuesPage(target,id,issueHooks()):fixPage(target,id);
    target.append(empty('Restricted page','Rota issues are for the people who plan and review rotas.',button('Go to the schedule',()=>navigate('schedule'),'secondary'),'lock'));return;}
  if(routeParts[0]==='benchmark'){if(scoreCheckOn())return scoreCheckPage(target,scoreCheckHooks());
    target.append(empty('Not available','Score checking is off for this department.',button('Go to the schedule',()=>navigate('schedule'),'secondary'),'lock'));return;}
  // A draft's Undo redraws reading only the draft (patchDraft); if the rest could not be kept, the page is read again.
  undoInPlace(done=>done.every(d=>d.kind==='workspace.undo'||d.kind==='workspace.redo')&&!!plannerMemo&&redraw().then(()=>true));
  // On a phone the Schedule is your published duties, whatever else you do: planning opens on a desktop (phone scope).
  const planner=permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)&&(!phoneLite()||!!phoneReview&&workspaceId===phoneReview&&acts('submitted')),zoneNow=zone(),today=toLocal(new Date().toISOString(),zoneNow).slice(0,10),ws=me!.organisation.week_start;
  // A view change, period move or search reuses what this visit already read (rota list, saved views, the draft, published
  // windows): no request. Any save, refresh or live update reads again (a conditional GET answers 304 when unchanged).
  const memo=reuseFor===renderSerial&&plannerMemo&&plannerMemo.offset===workspaceListOffset?plannerMemo:null;
  // Year and Month draw from day summaries: the draft's sessions are read only when an action needs them.
  const needDraft=schedView!=='year';
  // The draft is reused while you page through time or search in one view; a view change asks again with its ETag (304
  // when unchanged), so an edit made elsewhere (another tab) shows on the next view even before the live update arrives.
  // A rota that fails to load is this page's failure (This page didn't load, Retry, Show the schedule without this rota),
  // never "Couldn't refresh" over the page still on screen, which is another rota or view (S-28).
  const rotaFailed=(e:unknown)=>e instanceof ApiError&&(e.status===0||e.status>=500)&&!sessionChanged(e)?Object.assign(new Error(e.message),{cause:e}):e;
  const readDraft=(id:string)=>wholeRota(id).catch(e=>{throw rotaFailed(e);});
  // Week, Day and Month on a rota not yet read whole this visit (no link to one session, no comparison, proposal or
  // selection, not on a phone): the window first (wholeRota above). H-04: every entry route, Home's Review
  // (?review=proposal, Month with Auto-schedule open) too: the panel needs the whole rota and joins when it lands.
  const windowFirst=(id:string)=>!!id&&['week','day','month'].includes(schedView)&&!wholeSeen.has(id)&&!routeShift&&!routeQuery.get('shift')&&
    routeQuery.get('panel')!=='session'&&layerMode==='edit'&&!proposal&&!selected.size&&!phoneQuery.matches&&!phoneReview&&!sourcePublication;
  const warm=(id:string)=>!!memo&&scheduleCache.drafts.has(id)&&draftReadIn===schedView;
  // Staff never open drafts: a shared link naming one shows the published schedule (and says so once).
  if(!planner&&workspaceId){if(!phoneLite())notify('That rota is not available. Showing the published schedule.','warning');workspaceId='';}
  // The draft is asked for once: its failure is kept and handled below, not asked again.
  let earlyError:unknown=null;
  const early=(id:string)=>!id?Promise.resolve(undefined):warm(id)?Promise.resolve(scheduleCache.drafts.get(id)):needDraft&&!windowFirst(id)?readDraft(id).catch(e=>{earlyError=e;return undefined;}):Promise.resolve(undefined);
  if(!memo&&!ahead)readAhead();
  const earlyId=workspaceId,earlyData=early(earlyId);
  // The open rota's auto-schedule jobs are asked for with its sessions, not after the page is drawn (one round trip
  // less before the Auto-schedule panel can show its setup or a run in progress); the panel takes this read once.
  jobsEarly=planner&&needDraft&&earlyId&&permits(CAPABILITY.COORDINATOR)&&!windowFirst(earlyId)&&!(memo&&lastJobs?.id===earlyId&&lastJobs.activity===seenActivity)
    ?{id:earlyId,woke:panelWakes,read:get<Row>('/api/jobs?workspace_id='+encodeURIComponent(earlyId)).catch(()=>null)}:null;
  const savedViews=memo?.views?Promise.resolve(memo.views):fetchViews<PlannerSaved>('planner').catch(()=>[] as SavedView<PlannerSaved>[]);
  const listAhead=!memo&&!workspaceListOffset&&ahead?.id===workspaceId&&ahead.epoch===scheduleEpoch?ahead.list:null;
  // An automatic rota choice also writes the address. Wait for the current list instead of choosing from a kept reply.
  const readList=workspaceId||draftNone||sourcePublication?get:background;
  const page=planner?memo?.page||await (listAhead||readList<RotaList>(`/api/workspaces?limit=50&offset=${workspaceListOffset}`)):{workspaces:[] as Row[],total:0,has_more:false};
  const work:Row[]=[...page.workspaces];
  if(routeView){const saved=(await savedViews).find(x=>x.id===routeView);if(saved){applyPlannerView(saved.definition.planner,saved.definition.conditions,routeQuery);setTableColumns('planner-list',saved.definition);plannerViewId=saved.id;if(routeQuery.has('person'))schedFilters={...schedFilters,person_ids:[routeQuery.get('person')||'']};}routeView='';}
  // The Schedule's first opening this session shows its default view (the person's, else the department's), if chosen.
  else if(!memo){const open=openingView('planner',await savedViews);if(open!==undefined&&(open?.id||'')!==plannerViewId){applyPlannerView(open?.definition.planner,open?.definition.conditions,routeQuery);setTableColumns('planner-list',open?.definition);plannerViewId=open?.id||'';}}
  // An explicit publication is an exact version, including one outside the newest publications list.
  // Missing or forbidden versions show the normal read failure; never silently substitute a different rota.
  let source:PublicData|null=null;
  const readSource=(id:string)=>{if(memo&&scheduleCache.source?.id===id)return scheduleCache.source.read;
    const read=get<PublicData>('/api/publications/'+encodeURIComponent(id)),kept={id,read};scheduleCache.source=kept;
    read.catch(()=>{if(scheduleCache.source===kept)scheduleCache.source=null;});return read;};
  if(sourcePublication){
    source=await readSource(sourcePublication);
    if(!planDay)planDay=String(source.publication.starts_on);
  }
  let selectedData:WorkspaceData|undefined;
  if(workspaceId&&!work.some(w=>w.id===workspaceId)){
    // A link to a rota this account cannot open (or that no longer exists) opens the schedule without it.
    // A rota past the list's first page is read through the draft cache (readDraft), as every other draft is: a plain read
    // here was never kept, so each filter, search or layout redraw read the whole draft again (maintenance, seconds at scale).
    const first=await earlyData;
    selectedData=first||await (earlyError&&earlyId===workspaceId?Promise.reject(earlyError):readDraft(workspaceId)).catch(e=>{if(e instanceof ApiError&&[403,404,422].includes(e.status)){notify('That rota is not available. Showing the schedule without it.','warning');return undefined;}throw e;});
    if(selectedData)work.unshift(selectedData.workspace);else{workspaceId='';draftNone=true;}
  }
  // A published rota is the published layer, not a draft: an old link to one opens the schedule on its days.
  const named=work.find(w=>w.id===workspaceId);
  if(named&&!LIVE.includes(named.status)){
    if(!planDayFromRoute)planDay=String(named.starts_on);workspaceId='';draftNone=true;selectedData=undefined;
    if(!named.publication_id)throw new Error('This rota has changed. Search again to open its published version.');
    sourcePublication=String(named.publication_id);source=await readSource(sourcePublication);
    // Amended and published again since: its days as published now, not the version it replaced.
    if(!source.publication.current){sourcePublication='';source=null;}
  }
  // Year is the current published overview, not a rendering of one immutable publication.
  if(schedView==='year'&&source){sourcePublication='';source=null;draftNone=true;notify('Year shows currently published rotas.','info');}
  const drafts=work.filter(w=>LIVE.includes(w.status)&&!w.archived_at);
  // Open on what this person acts on: coordinators their draft, reviewers the rota waiting for review (nothing about rotas is kept in browser storage).
  // A link to a day opens a rota that covers the days it shows: a draft in that period (the editable one first, then
  // the newest of a state; the others show as "N other drafts"), else the published rota. Never a draft elsewhere in time.
  // shortcut: "newest" is the list order (created_at desc); the API has no last-changed time. Use one if it is added.
  // Undated, it opens today's week of the rota covering today: a draft on those days, else the published rota (a draft
  // elsewhere in time only through the draft switcher); with nothing published today, the draft you act on.
  // The choice is made once: a view change (Year, Month…) keeps the source, published or draft (draftNone below).
  if(!workspaceId&&!draftNone&&planner&&!source){const firstStates=defaultRotaStates(me!.roles);
    // The days of the default Week around the anchor, whatever view opens (S-30): an undated Year or Month link must not
    // pick a draft months away and jump there.
    // A dated Year or Month link keeps the day it names: only a draft covering that day, not its week (LIVE-43).
    const shown=planDayFromRoute&&['year','month'].includes(schedView)?[planDay]:periodFor('week',planDay||today,ws).days,covering=drafts.filter(w=>shown.some(d=>w.starts_on<=d&&d<w.ends_on)&&
      // F-11/F19: an undated Schedule over a published week shows it as published; an amendment opens from its link or the switcher.
      !(w.base_publication_id&&!planDayFromRoute));
    const publishedToday=!planDayFromRoute&&work.some(w=>w.status==='published'&&w.starts_on<=today&&today<w.ends_on);
    const pool=planDayFromRoute||publishedToday||covering.length?covering:drafts;
    workspaceId=(firstStates.map(st=>pool.find(w=>w.status===st)).find(Boolean))?.id||'';if(workspaceId)jumpToDraft=true;else if(planDayFromRoute||drafts.length)draftNone=true;
    draftWhy=!workspaceId?'':planDayFromRoute?'covers the linked day':pool===covering?'covers today':`your newest ${pool.find(w=>w.id===workspaceId)?.status==='draft'?'draft':'rota in review'}`;}
  const cached=workspaceId?scheduleCache.drafts.get(workspaceId):undefined;
  let draftData:WorkspaceData|null=!workspaceId?null:selectedData||(earlyId===workspaceId?await earlyData:undefined)||(warm(workspaceId)||!needDraft)&&cached||null;
  // An amendment opens on its change's week (change_diff, in the whole detail only): it is read whole.
  const pre=workspaceId?takeAhead(workspaceId):null;
  let meta=workspaceId&&!draftData&&needDraft&&windowFirst(workspaceId)?await (pre?.meta||rotaMeta(workspaceId)).catch(e=>{throw rotaFailed(e);}):null;
  if(meta?.workspace.base_publication_id)meta=null;
  // The whole rota read ahead may already be in: drawn whole at once, with no window first.
  if(meta&&scheduleCache.drafts.has(meta.workspace.id)){draftData=scheduleCache.drafts.get(meta.workspace.id)!;wholeSeen.add(meta.workspace.id);meta=null;}
  if(workspaceId&&!draftData&&needDraft&&!meta)draftData=await readDraft(workspaceId);
  // Year and Month without a loaded draft use the draft's row from the rota list (name, dates, status, health summary).
  // The compact detail (meta) stands in only until the rota is read: it has no review_snapshot nor validation_current,
  // so the rota's facts are always the read rota's once there is one (below, after the window).
  let w=draftData?.workspace||meta?.workspace||(workspaceId?work.find(r=>r.id===workspaceId)||null:null);
  // Whole-rota work never runs on the window: it waits for the whole rota.
  const withDraft=(f:(d:WorkspaceData)=>unknown)=>()=>(draftData&&!partial(draftData)?Promise.resolve(draftData):readDraft(w!.id)).then(f).catch(reportError);
  // Picking a draft brings its days into view; a date in the address wins.
  // (With no day chosen yet, a draft that covers today opens on today's week.)
  // A ?shift= link (Resolve issues › Show on schedule, a message) names a session of this draft: it opens on the session's
  // current day (it may have moved since the link was made); one no longer in this draft is said, never marked silently.
  if(!routeShift&&shiftLink&&draftData&&!partial(draftData)&&String(draftData.workspace.id)===shiftLink.rota){const s=draftData.shifts.find(x=>String(x.id)===shiftLink!.id);
    if(!s||toLocal(s.start,zoneNow).slice(0,10)!==shiftLink.day)routeShift=shiftLink.id;}
  if(routeShift&&draftData){const s=draftData.shifts.find(x=>String(x.id)===routeShift);
    if(s){const day=toLocal(s.start,zoneNow).slice(0,10);if(linkMoved(planDay,day,ws,routeQuery.get('panel')!=='session'))notify(SESSION_MOVED(formatDay(s.start,zoneNow)),'info');planDay=day;}
    else{notify(SESSION_REMOVED,'warning');routeShift='';shiftLink=null;routeQuery.delete('shift');routeQuery.delete('panel');}}
  // F4 (maintenance): an amendment opens on the week of its first change (review_week_start), not on its first day.
  const changedWeek=String((draftData as {change_diff?:{review_week_start?:string|null}|null}|null)?.change_diff?.review_week_start||'');
  // ME-26: picking an amendment always opens its change's week, even when the day in view is already inside it.
  const anchor=planDay||today,amends=!!changedWeek&&!!w?.base_publication_id;
  if(w&&jumpToDraft&&!planDayFromRoute&&(amends||!(anchor>=w.starts_on&&anchor<w.ends_on)))planDay=amends?changedWeek:String(w.starts_on);
  jumpToDraft=false;planDayFromRoute=false;if(!planDay)planDay=today;
  if(meta){draftData=await readWindow(meta,periodNow(planDay,ws).days).catch(e=>{throw rotaFailed(e);})||await readDraft(meta.workspace.id);
    if(!partial(draftData))w=draftData.workspace;}  // read whole instead (it changed meanwhile): its own facts, not meta's
  const whole=draftData&&!partial(draftData)?draftData:null;
  // D59: the window on screen, the whole rota on its way. Every write control is held where it stands (never queued).
  const reading=partial(draftData),hold=<T extends HTMLElement>(n:T):T=>reading?notYet(n,WHOLE_LOADING):n;
  let wholeBusy:HTMLElement|null=null;  // the footer's busy pill while reading: gone if the whole read fails
  // A shared sandbox someone else owns is view only (D18): copy it to change it.
  const draftAccess=!!w&&w.status==='draft'&&permits(CAPABILITY.COORDINATOR)&&editableDraft(w,me!.identity_id);
  const draft=draftAccess&&!w!.archived_at,fresh=!!draftData&&w!.validation_current===true;
  if(draft&&w)requestBaselineDrift(w);
  // The server says a check job exists (a provisional check): join it, so the status shown is that job's own.
  if(draft&&whole&&(!fresh||serverChecking(w!.validation)))autoCheck(whole);
  plannerRota=w;
  const period=periodNow(planDay,ws) as Period&ReturnType<typeof calendarPeriod>;
  // Add session starts on the first day in view that is in the rota, not the rota's first day (LIVE-37).
  const shownDay=(d:WorkspaceData)=>({day:period.days.find(x=>x>=d.workspace.starts_on&&x<d.workspace.ends_on)||d.workspace.starts_on});
  // SW3-A-02: New rota from a view with no rota offers the dates in view (a month view: that whole month), not the
  // next free period after the latest rota (Home's New rota keeps that).
  const inView=()=>{const month=rangeNow()==='month',first=month?period.days[0].slice(0,7)+'-01':period.days[0];
    return {name:'',starts_on:first,ends_on:month?addDays(first,31).slice(0,7)+'-01':addDays(period.days.at(-1)!,1)};};
  // The view stays on screen while the next one loads, marked busy (dimmed after 300 ms by CSS).
  // The selection is kept across periods (SI-15; cleared when the rota changes): the bar counts the ones out of view.
  const go=(anchor:string)=>{planDay=anchor;document.querySelector('#schedule-view')?.setAttribute('aria-busy','true');void redraw();};
  const sideBySide=layerMode==='side'&&!!w;
  // The published layer for the days in view (Year and Month read day summaries instead).
  const windowed=schedView!=='year'&&!sideBySide&&!source;
  // One window per range: a range seen this visit is reused, and two draws asking at once share one request.
  // 24 ranges, not fewer: api.ts's ETag cache holds the same replies, so a smaller cap frees no memory (measured
  // 6 Nov 2026: 22.7 MB after 30 months at 24 or 8) and only adds a revalidation on going back (MEMLEAK round 2).
  const range=`start=${period.days[0]}&end=${addDays(period.days.at(-1)!,1)}`;
  if(windowed&&!(memo&&scheduleCache.windows.has(range))){
    if(scheduleCache.windows.size>=24)scheduleCache.windows.delete(scheduleCache.windows.keys().next().value!);
    // A failed read: with a draft, the draft still shows (and a toast says what is missing); without one, the view says so.
    const read=(pre?.range===range&&pre.window||get<ScheduleWindow>('/api/schedule/sessions?'+range)).catch(e=>{scheduleCache.windows.delete(range);if(sessionChanged(e))throw e;
      if(draftData)notify(couldntLoad('the published sessions',e),'error',undefined,e);return {failed:(e as Error).message,cause:e as unknown};});
    scheduleCache.windows.set(range,read);}
  const got=windowed?await scheduleCache.windows.get(range) as ScheduleWindow|{failed:string;cause?:unknown}|null:null;
  const failed=got&&'failed' in got?got.failed:'',cause=got&&'failed' in got?got.cause:undefined,win=failed?null:got as ScheduleWindow|null;
  if(mySerial!==renderSerial)return;
  // The issued check as stored (team readers; staff get only whether it was valid): never a fresh check of the source.
  const issued=source?.original_validation as {coverage?:Row[];findings?:Row[]}|undefined;
  const sourceWindow:ScheduleWindow|null=source?{starts_on:source.publication.starts_on,ends_on:source.publication.ends_on,summary:source.summary,sources:[{...source.publication,operational:source.operational}],shifts:source.snapshot.shifts as PlannerSession[],coverage:issued?.coverage||[],findings:issued?.findings||[],people:source.snapshot.people,locations:source.snapshot.locations,services:source.snapshot.services}:null;
  const layer=sourceWindow||win,team=may('schedule.read_team');
  // Staff have no catalogues: names come with the window (only what it shows).
  const sctx=():Context=>({...ctx(),catalogues:{...catalogues,people:merged(rows('people'),layer?.people) as PersonRow[],locations:merged(rows('locations'),layer?.locations),services:merged(rows('services'),layer?.services)}});
  // SX-04 (D38): the published grid counts the change list as staged (an absence's open place shows at once).
  const liveCheck=!draftData&&publishedLive?.key===liveKey(layer)?publishedLive:null;
  const shown=layered(draftData||null,layer,layerMode,period,zoneNow,liveCheck?stagedCoverage(liveCheck.evaluation as unknown as {coverage:Row[]}) as unknown as Evaluation:null);
  if(!draftData&&!(source?may('publications.read_team'):team))shown.participantsFiltered=true;
  // Live coverage of the published days (open places from leave recorded since): planners, and staff for their own duties.
  // SW-15: only roles the server lets read it (publications.read_team, else a staff member's own duties), never a 403.
  if(!draftData&&(permits(CAPABILITY.PUBLICATIONS_READ_TEAM)||permits(CAPABILITY.STAFF))&&layer&&!source)void loadLiveCoverage(layer);
  // Changes to a published rota (B1, B2): a coordinator changes a current publication's sessions from their panel; the
  // top bar's chip opens its change list. Only publications still running are read.
  const changing=permits(CAPABILITY.COORDINATOR)&&!!layer&&(!source||!!source.publication.current);
  const pubHooks:PublishedHooks|null=changing?{zone:zoneNow,name:id=>fullNameOf(sctx().catalogues.people.find(p=>p.id===id)),place:s=>labelOf(sctx().catalogues.locations.find(l=>l.id===s.location_id)),
    sessions:()=>source?layer!.shifts.map(s=>({...s,publication_id:source!.publication.id})):layer!.shifts,sources:()=>layer!.sources,redraw:()=>void redraw(),open:s=>readOnly(s)}:null;
  const changePubs=changing?layer!.sources.filter(x=>x.kind!=='draft'&&String(x.ends_on)>today).map(x=>String(x.id)):[];
  if(pubHooks)loadChanges(changePubs,()=>void redraw());
  const changeChip=pubHooks?changesChip(changePubs,pubHooks,w?.id):null;  // L11: not for the amendment on screen (its badge says its state)

  // ---- Top bar: the editable draft (or the page title), its status and its actions ----
  const switcher=el('button',{type:'button',class:'rota-switcher',...popup('dialog'),'aria-label':w?'Switch draft · '+w.name:'Choose a draft'},el('h1',{},w?w.name:'Schedule'),icon('chevronDown'));
  const chooseDraft=(id:string)=>{draftWhy='';workspaceId=id;sourcePublication='';draftNone=!id;draftsShown=!!id;selected.clear();jumpToDraft=true;if(compareId===id)compareId='';if(!id)layerMode='edit';renderPage();};
  // The shared picker (picker.ts): search, keyboard and placement as every dropdown; the drafts' actions sit under the list.
  const NO_DRAFT='published-only';
  const openDrafts=(query='')=>{
    const order:Record<string,number>={draft:0,submitted:1,approved:2};
    const rank=(r:Row)=>DRAFT_GROUPS.indexOf(draftGroup(r,me!.identity_id));
    const sorted=[...drafts].sort((a,b)=>rank(a)-rank(b)||(order[a.status]??5)-(order[b.status]??5)||String(b.starts_on).localeCompare(String(a.starts_on)));
    let close=()=>{};
    const act=(label:string,glyph:IconName,run:()=>void)=>menuItem(label,glyph,()=>{close();run();},{role:'button'});
    const foot=el('div',{class:'popover-foot','data-drafts':''},
      // maintenance: a private copy of the rota shown, opened at once (no dialog).
      w&&permits(CAPABILITY.COORDINATOR)?menuItem('New private draft','fork',()=>{close();void newPrivateDraft(w,drafts.filter(x=>x.purpose==='sandbox'&&x.owner_identity_id===me!.identity_id)).then(x=>chooseDraft(String(x.id)),reportError);},{role:'button'},'Copy of '+w.name):null,
      permits(CAPABILITY.COORDINATOR)?act('New rota','plus',()=>workspaceForm(ctx(),undefined,w?undefined:inView())):null,
      permits(CAPABILITY.COORDINATOR)?act('Archived rotas','history',()=>void archivedRotas(me!.identity_id,restoredRota).catch(reportError)):null,
      permits(CAPABILITY.COORDINATOR)&&scoreCheckOn()?act(scoreCheckTitle(),'upload',openScoreCheck):null,
      page.has_more||workspaceListOffset>0?el('div',{class:'actions'},el('span',{},`${page.total} rotas · page ${workspaceListOffset/50+1}`),
        button('Previous rotas',()=>{workspaceListOffset=Math.max(0,workspaceListOffset-50);close();renderPage();},'small',{disabled:workspaceListOffset===0,why:PAGE_WHY.first}),
        button('Next rotas',()=>{workspaceListOffset+=50;close();renderPage();},'small',{disabled:!page.has_more,why:PAGE_WHY.last})):null);
    // Grouped by what you can do with them (SR-01): drafts are edited; rotas in review are read; published days need neither.
    // An amendment says what it amends (SR-02); the rota the page chose by itself says why (SR-03).
    const why=(r:Row)=>[r.base_publication_id?'Amends the published rota':'',r.id===w?.id&&draftWhy?'Opened: '+draftWhy:''];
    close=openPicker(switcher,switcher.parentElement||document.body,{label:'Drafts',multiple:false,query,recentKey:'drafts',selected:new Set([w?.id||NO_DRAFT]),extra:foot,
      choices:[...sorted.map(r=>rotaChoice(r,why(r))),{value:NO_DRAFT,label:'Published only',group:'Published',hint:'No draft on the schedule'}],
      lookup:rotaLookup,onPick:id=>chooseDraft(id===NO_DRAFT?'':id)});
  };
  switcher.addEventListener('click',()=>{if(switcher.getAttribute('aria-expanded')!=='true')openDrafts();});
  // A live redraw (e.g. published coverage arriving) keeps an open Drafts list open, with its search, on the new button.
  const draftsPicker=()=>document.querySelector('[data-drafts]')?.closest<HTMLElement>('.picker')||null;
  const keptDrafts=draftsPicker(),keptQuery=keptDrafts?.querySelector<HTMLInputElement>('.picker-search')?.value||'';
  if(keptDrafts)queueMicrotask(()=>{if(switcher.isConnected&&!draftsPicker())openDrafts(keptQuery);});
  const workflow=el('div',{class:'actions rota-decisions'});
  let undoButton:HTMLButtonElement|null=null,redoButton:HTMLButtonElement|null=null,rotaHealth:HTMLElement|null=null,statusPill:HTMLElement|null=null,auto:HTMLButtonElement|null=null,help:HTMLButtonElement|null=null,tools:HTMLButtonElement|null=null,driftButton:HTMLButtonElement|null=null;
  const currentAuto=()=>document.querySelector<HTMLButtonElement>('.auto-button');
  const closeAuto=()=>{schedulingSettings.open=schedulingSettings.editing=false;const panel=document.getElementById('auto-panel');if(panel)panel.hidden=true;currentAuto()?.setAttribute('aria-expanded','false');};
  const returnFromAuto=()=>{closeAuto();currentAuto()?.focus();};
  const side=sidePanel(el('aside',{class:'auto-panel',id:'auto-panel','aria-label':'Auto-schedule'}),{key:'auto-schedule',width:420,
    close:returnFromAuto,dismiss:closeAuto});
  const autoOpen=draft&&schedulingSettings.open;
  if(w){const data=whole;
    const drift=baselineDriftFor(w);driftButton=draft&&drift?.changes.length?hold(baselineDriftButton(w,drift.changes.length)):null;
    // Drafts check themselves after every change (autoCheck). When that could not run or is outdated, the footer's
    // "Needs a check" pill runs it; no separate Check rota button.
    const validate=el('button',{type:'button',class:'health-pill warn','data-tip':'The rota or its rules changed since the last check · select to check now'},icon('clock'),el('span',{class:'label'},TERMS.needsCheck));
    footerAct(validate,'needs-check',async()=>{
      // A check that hasn't started is set aside as Cancel does (its queued job stopped, audited), then checked here and now.
      if(checkJob&&(checkStalled()||!!checkUnstarted)){checkCancelled=autoCheckKey;autoChecking=false;void stopJob(checkJob.id);checkJob=null;}else{autoCheckKey='';checkCancelled='';}
      checkStop=null;checkUnstarted='';validate.disabled=true;validate.lastElementChild!.textContent='Checking…';try{await command('workspace.validate',{reason:'Routine rota check'},w.id,w.revision);await refresh();
      const v=plannerRota?.id===w.id?plannerRota.validation:null;notify(v?(v.error_count?`Checked: ${v.error_count} must fix, ${v.warning_count} could improve.`:'Checked: nothing must be fixed.'):'Checked.',v?.error_count?'warning':'success');}
      catch(e){validate.disabled=false;validate.lastElementChild!.textContent=TERMS.needsCheck;reportError(e);}});
    // Whoever created or last edited the rota cannot approve it; say so instead of offering a button that fails.
    const ownWork=!mayDecide(me!,'rota.approve',data?.reviewer_involved??[w.created_by,w.last_edited_by].includes(me!.identity_id));  // the server's contributor test
    // Submitted, but rules, people or availability changed since: approving or publishing would be refused.
    // the window carries the compact detail's review_progress, so Week reads the same server field as the whole rota.
    const outdated=rotaOutdated(w,draftData);
    // L11: out of date is the rota's one status (rotaStatusPill), not a second pill; the review strip's "?" says why.
    // #1041: one opening of the review per rota; a second press, or a redraw's button, joins it (held, busy) until it opens.
    const approveLabel=w.base_publication_id?'Approve changes':'Approve rota',approveWords=busyLabel(approveLabel);
    const openApproval=()=>joinRun(reviewRuns,w.id,withDraft(async d=>{if(!decidesStep(d))return notDecider(d);const read=await readRotaReview(d);return approvalDialog(ctx(),d,form=>rotaReviewSummary(form,d,read,'approve'));}),
      ()=>notify(`${WAIT_WORDS.slow} the review…`,'info'));
    const approveButton=():HTMLButtonElement=>{const b=button(approveLabel,openApproval,'primary keep-label'),run=reviewRuns.get(w.id);if(!run)return b;
      notYet(b,approveWords).classList.add('is-busy');b.setAttribute('aria-busy','true');b.replaceChildren(el('span',{class:'spinner','aria-hidden':'true'}),approveWords);
      void run.then(()=>{if(!b.isConnected)return;const why=b.getAttribute('aria-describedby');b.replaceWith(approveButton());if(why)document.getElementById(why)?.remove();});return b;};
    // D25 (maintenance): Home's "Rota to review" (?approve=<rota>) opens the review at once, on a phone too; asked once.
    if(routeQuery.get('approve')===w.id){routeQuery.delete('approve');history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;
      if(w.status==='submitted'&&!w.archived_at&&!outdated&&permits(CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)&&!ownWork)void openApproval();}
    // SX-01 (maintenance can_decide): Approve only for whoever decides the pending step (its role, escalation role or a
    // delegation, independent of the rota); everyone else sees none.
    if(w.status==='submitted'&&!w.archived_at&&!outdated&&permits(CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)){
      if(ownWork)workflow.append(el('span',{class:'health-pill neutral','data-tip':'You created or edited this rota, so another approver must review it.'},icon('lock'),'Waiting for another approver'));
      else if(data&&decidesStep(data))workflow.append(approveButton());}
    // Review can send it back: an approver returns a rota in review to the coordinator as a draft, with a reason.
    if(w.status==='submitted'&&!w.archived_at&&permits(CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)&&!permits(CAPABILITY.COORDINATOR))workflow.append(button(TERMS.returnToCoordinator,()=>confirmAction(TERMS.returnToCoordinator,'The rota goes back to draft so the coordinator can change it, check it and submit it again. Say what needs to change: the coordinator is told, and the reason is kept in the audit log.',async reason=>{await command('workspace.reopen',{reason},w.id,w.revision);notify('Returned to the coordinator.');await refresh();},TERMS.returnToCoordinator),outdated?'primary':'secondary'));  // out of date it is the approver's only action
    // Publish…: one entry point for the whole way to staff. It checks first, then submits a draft for review or publishes an approved rota.
    // Named after the step that happens next: a draft is submitted for review; an approved rota is published.
    const step=w.status==='draft'?'Submit for review…':'Publish…',sandbox=w.purpose==='sandbox',rota=sandbox?rotaFor(w,work):null;
    // maintenance: your own draft merges into the period's rota instead of being submitted (a sandbox is never submitted).
    if(w.archived_at){
      if(lifecycleOf(w)==='workspace.restore')workflow.append(button('Restore',()=>restoreRota(w,restoredRota).catch(reportError),'secondary'));
    }else if(sandbox&&draft&&rota)workflow.append(compact(button('Merge into rota…',()=>void mergePanel(w,{rota,time:x=>formatTime(x,zone()),day:x=>formatDay(x,zone()),
      ring:ids=>{for(const n of document.querySelectorAll('.cause-ring'))n.classList.remove('cause-ring');for(const id of ids)for(const n of document.querySelectorAll(`[data-session~="${CSS.escape(id)}"]`))n.classList.add('cause-ring');},
      done:id=>chooseDraft(id),person:id=>name('people',id),before:(f=>f?{must:f.must,places_open:f.places_open}:null)(fixSummaryOf(rota.health?.summary))}),'secondary',{'aria-label':'Merge into rota…'}),'fork'));
    // SW3-A-11 (D41): no rota for the period to merge into is said where Merge is, with who can fix it (never a missing
    // button). aria-disabled keeps its focus and tip; pressing it says why, with the setting for an admin.
    else if(sandbox&&draft&&permits(CAPABILITY.COORDINATOR)){const why=mergeBlocked();
      workflow.append(compact(button('Merge into rota…',()=>notify(why+'.','info',permits(CAPABILITY.ADMIN)&&!rotaCoordinatorIds.length?{label:'Choose Rota Coordinators',run:()=>navigate('configuration/'+addressOf('scheduling_groups'))}:undefined),'secondary',{'aria-label':'Merge into rota…','aria-disabled':'true','data-tip':why}),'fork'));}
    else if(!sandbox&&permits(CAPABILITY.COORDINATOR)&&(w.status==='draft'||(w.status==='approved'&&!outdated)))workflow.append(compact(button(step,withDraft(publishDialog),'secondary',{'aria-label':step,'data-busy':'Checking…','data-tip':w.status==='draft'?'Check it, then send it to an approver. Publish it here once approved.':'Check again, then publish to staff'}),'send'));
    else if(w.status==='approved'&&!outdated&&permits(CAPABILITY.APPROVER))workflow.append(compact(button('Publish…',withDraft(publishDialog),'secondary',{'aria-label':'Publish…','data-busy':'Checking…'}),'send'));
    // One predicate for Reopen: the note below shows exactly when the button does not.
    const mayReopen=!w.archived_at&&['submitted','approved'].includes(w.status)&&permits(CAPABILITY.COORDINATOR);
    if(mayReopen)workflow.append(outdated
      // Outdated: the reason is known, so the note is optional.
      ?button('Reopen as draft',()=>confirmAction('Reopen as draft','Rules, people or availability changed since it was submitted. Reopen it, check it and submit it again. Published rotas are not changed.',async reason=>{await command('workspace.reopen',{reason:reason==='Routine update'?'Rules, people or availability changed since it was submitted':reason},w.id,w.revision);notify('Reopened as a draft. Check it, then submit it again.');await refresh();},'Reopen as draft',undefined,true),'primary')
      :button('Reopen as draft',()=>confirmAction('Reopen as draft','Reopening invalidates review and approval. Published history is not changed.',async reason=>{await command('workspace.reopen',{reason},w.id,w.revision);notify('Reopened as a draft.');await refresh();},'Reopen as draft'),'secondary'));
    // an approved rota out of date waits on its coordinator (only they reopen an approved rota): say so where the actions are.
    if(!w.archived_at&&outdated&&w.status==='approved'&&!mayReopen)workflow.append(el('span',{class:'health-pill info','data-tip':'Rules, people or availability changed after approval. The coordinator reopens it, checks it and submits it again.'},icon('clock'),'Waiting for the coordinator'));
    for(const b of [...workflow.children]){compact(b);if(b instanceof HTMLButtonElement)hold(b);}
    statusPill=(!w.archived_at&&sandboxPill(w,me!.identity_id))||rotaStatusPill(w,outdated);
    // Make this the rota… is a configured Rota Coordinator's (planning settings): read before Rota tools opens.
    // SW3-A-11: a private draft with no rota says whether Rota Coordinators exist; a list that changes redraws it.
    if(permits(CAPABILITY.COORDINATOR))void rotaCoordinators().then(ids=>{const changed=ids.join()!==rotaCoordinatorIds.join();rotaCoordinatorIds=ids;if(changed&&sandbox&&!rota)void redraw();},()=>{});
    // Owner decision: no "N rules" chip or Resolve button in the top bar (footer pills reach problems). Both live in
    // Rota tools: Rules in this rota (rules in context) and Resolve one by one (with the count).
    // Steps, not places or findings: each open session and each person on a session who breaks a mandatory rule.
    const toResolve=draft&&fresh&&whole&&permits(CAPABILITY.COORDINATOR)?resolveQueue(whole).length:0;
    // The Fix list's counts (causes, the server's): the footer, Rota tools and Home say the same numbers.
    // The chosen rota's own row (`named` is looked up before a plain Schedule visit picks its default draft).
    // QA cycle 3 (maintenance): one source. The Fix list held for the rota as it is now (the panel's, the page's, an edit's delta)
    // is the footer's too; the rota list's summary only until one is held.
    const fixSum=heldFixSummary(w.id,scheduleCache.drafts.get(w.id)||null)||fixSummaryOf(work.find(r=>r.id===w.id)?.health?.summary);footerFrom.set(w.id,fixSum);
    const resolveNow=()=>{const first=resolveQueue(draftData!)[0];startResolve(w.id);schedulingSettings.open=schedulingSettings.editing=false;if(first)toCalendar(first.shift);renderPage();};
    // Archived drafts can show saved runs. Only a live draft can capture a baseline or run.
    const missingBaseline=draft&&!w.baseline;
    auto=draftAccess?(missingBaseline
      ?hold(button('Capture baseline',()=>void captureDraftBaseline(w,refresh).catch(reportError),'secondary',{'aria-label':'Capture baseline','data-tip':'Capture this draft\'s immutable inputs before auto-schedule'}))
      :el('button',{type:'button',class:'button secondary auto-button'+(whole&&proposalShown(whole)?' has-proposal':''),
        // D59: whole-rota work waits for the whole rota: held in place, focusable, saying why (notYet).
        'aria-label':!whole?'Auto-schedule · loading the whole rota…':proposalShown(whole)?'Auto-schedule · proposal ready':'Auto-schedule','aria-expanded':String(autoOpen),'aria-controls':'auto-panel','data-tip':draft?'Auto-schedule · fill open places using your rules':'Auto-schedule · view saved runs'},icon('sparkle'),el('span',{class:'label'},'Auto-schedule'))):null;
    if(auto&&!whole)notYet(auto,WHOLE_LOADING);
    side.hidden=!autoOpen;
    // Help reads the views that draw sessions; Year and Month (day health) have their own key.
    if(schedView!=='year')help=iconButton('How to read this schedule','help',()=>unpin(()=>toggleHelp(w.id,closeAuto)),{'aria-pressed':String(helpOpen())});
    // A check that stopped or was cancelled keeps the last results, marked "Not checked" (grey; select it to check again).
    // A check that stopped (failed, refused) or never started says so on the same pill, which checks again (visible,
    // gold, in words; never a spinner): the one wording rule (rule_check_api.ts checkingWords, DL-018). Refused for
    // the person's own access, it only says so: no action the person cannot perform.
    // `unstarted`: the page took the queued check over (once, by itself) and that failed too.
    const unstarted=draft&&(!fresh||serverChecking(w.validation))&&checkUnstarted.startsWith(w.id+':');
    const stalled=unstarted||autoChecking&&draft&&checkJob?.key.startsWith(w.id+':')===true&&checkStalled();
    const unfinished=unstarted?CHECK_NOT_STARTED:stalled?checkingWords(checkJob!.status,Date.now()-checkJob!.since,jobWorkers.get(checkJob!.id)):draft&&(!fresh||serverChecking(w.validation))&&checkStop?.rota===w.id?checkingWords('failed'):'';
    const refused=!stalled&&!!unfinished&&checkStop!.refused;
    // This person cancelled the latest revision's check: nothing is queued, so it is "Not checked", never "Waiting to check".
    const cancelled=!unfinished&&draft&&(!fresh||serverChecking(w.validation))&&!!checkCancelled&&checkCancelled===autoCheckKey&&autoCheckKey.startsWith(w.id+':');
    if(unfinished){validate.lastElementChild!.textContent=refused?unfinished:unfinished+' · '+CHECK_AGAIN;validate.disabled=refused;delete validate.dataset.tip;
      if(refused)validate.setAttribute('aria-describedby',CHECK_STOPPED_WHY);}  // DL-007: a disabled control says why (the line above the grid)
    else if(cancelled){validate.classList.replace('warn','neutral');validate.lastElementChild!.textContent='Not checked';}
    // A recheck keeps the last counts with a spinner (R1: counts never blank) beside its elapsed time and Cancel (B7).
    const checking=autoChecking&&draft&&(!fresh||!!checkJob||serverChecking(w.validation))&&!stalled,longCheck=checkLine;
    // maintenance: a stored check that covers only the last edit (someone's, its complete check queued with the workers) is
    // said to be checking, in the editor's words, with its counts kept, never shown as current; this page runs no check.
    // D-062 + Master 10 Nov: the scope only says a job exists to look at; the words are that job's own status, read
    // from the rota's job list this page holds. No live job found: nothing claims a check (`unproven`: the partial
    // check is not shown as current either).
    const partialCheck=!checking&&!stalled&&serverChecking(w.validation);
    const listed=partialCheck&&lastJobs?.id===w.id?((lastJobs.jobs.jobs||[]) as Row[]).find(j=>j.kind==='validation'&&['queued','running'].includes(j.status)):undefined;
    if(listed?.status==='queued'&&!jobWorkers.has(String(listed.id)))readWorkers(String(listed.id),w,()=>{if(!checkBusy())void redraw();});  // its bound, once
    const provisional=listed&&!checkUnfinished(listed.status,Date.now()-Date.parse(listed.created_at),jobWorkers.get(String(listed.id)))?listed:undefined,unproven=partialCheck&&!provisional;
    if(partialCheck&&!workersKnown())void reprobeWorkers(w.id+':'+w.revision);  // the stale "no worker" answer is asked again
    // A published rota has nothing to check in place (its changes are amendments): its footer is the published layer's.
    const kind=footerCheck({stalled,checking:!!checking,provisional:!!provisional,checked:!!w.validation,counts:!!fixSum,current:fresh,
      partial:unproven,jobsRead:lastJobs?.id===w.id,ended:!!unfinished||cancelled,whole:!!data,draft});
    const withWords=(words:string)=>fixSum?(p=>(p.append(el('span',{class:'muted fix-checking'},el('span',{class:'spinner','aria-hidden':'true'}),words)),p))(fixHealth(w.id,{...fixSum,checking:true},data,w))
      :el('span',{class:'health-pill neutral'},icon('clock'),words);
    rotaHealth=w.archived_at||outdated||footerIsPublished(w)?null:reading?wholeBusy=el('span',{class:'health-pill neutral'},el('span',{class:'spinner','aria-hidden':'true'}),WHOLE_LOADING):kind==='stalled'?(fixSum?staleHealth(fixHealth(w.id,{...fixSum,stale:true} as FixSummary,data,w),validate):validate)
      :kind==='checking'?(fixSum?(p=>(p.append(longCheck()),p))(fixHealth(w.id,{...fixSum,checking:true},data,w)):longCheck())
      :kind==='provisional'?withWords(checkingWords(provisional!.status)):kind==='waiting'?withWords(waitingWord('queued'))
      :kind==='not-checked'?el('span',{class:'health-pill neutral'},icon('clock'),TERMS.notChecked):kind==='counts'?fixHealth(w.id,fixSum!,data,w)
      :kind==='checked'?el('span',{class:'health-pill neutral'},icon('check'),TERMS.checked)
      :kind==='stale'?staleHealth(fixHealth(w.id,{...fixSum!,stale:true} as FixSummary,data,w),validate):kind==='check-now'?validate
      :el('span',{class:'health-pill warn','data-tip':'The rota or its rules changed since the last check.'},icon('clock'),TERMS.needsCheck);
    // Submit, Approve and Publish wait while the latest revision is not checked, whatever the check's state (running,
    // waiting, stalled, not started, cancelled, failed): held in place (D59 notYet), saying what the footer says.
    const checkHold=reading||!draft||fresh&&!serverChecking(w.validation)?'':kind==='checking'?checkSaid():kind==='provisional'?checkingWords(provisional!.status)
      :kind==='waiting'?waitingWord('queued'):kind==='not-checked'?TERMS.notChecked
      :['stalled','stale','check-now'].includes(kind)?validate.lastElementChild!.textContent!.split(' · ')[0]:TERMS.needsCheck;
    if(checkHold)for(const b of workflow.children)if(b instanceof HTMLButtonElement){notYet(b,checkHold);if(kind==='checking')b.dataset.checkHold='';}
    // Overlaps: an undecided published rota on its days, or competing drafts. Both open the same overlap panel.
    const overlap=w.overlap||named?.overlap||work.find(r=>r.id===w.id)?.overlap;
    const conflicts=overlap?.publications?.length||0,competing=overlap?.drafts?.length||0;
    if((conflicts||competing)&&rotaHealth){const b=el('button',{type:'button',class:'health-pill warn','aria-haspopup':'dialog','aria-label':conflicts?`${plural(conflicts,'overlap')} with a published rota`:`${plural(competing,'other draft')} for these days`,'data-ctip':conflicts?'Overlaps a published rota · choose how it fits':'Other drafts cover these days · compare'},
      el('span',{class:'sd-dot warn both','aria-hidden':'true'}),el('span',{class:'num'},String(conflicts||competing)),el('span',{class:'label'},conflicts?' overlap':' other draft'+(competing>1?'s':'')));
      footerAct(hold(b),'overlap',withDraft(overlapDialog));
      // Siblings in the non-interactive health group, never inside the Check button (AO-002: no nested control, no bubbled check).
      if(!rotaHealth.classList.contains('health-pills'))rotaHealth=el('div',{class:'health-pills',role:'group','aria-label':'Rota health'},rotaHealth);
      rotaHealth.append(b);}
    const absenceIds=draft&&whole?[...new Set<string>((w.validation?.findings||[]).filter((f:Row)=>['UNAVAILABLE','EXTERNAL_ROTATION'].includes(f.code)).map((f:Row)=>String(f.shift_id)))]:[];
    tools=iconButton('Rota tools','dots',()=>popover(tools!,(panel,close)=>{
      const free=(label:string,glyph:IconName,run:()=>unknown)=>menuItem(label,glyph,()=>{close();return run();}),item:typeof free=(...a)=>hold(free(...a));
      // Labelled sections, one icon per tool: check the rota, edit it, share it; discarding comes last on its own.
      const section=(label:string,...items:HTMLElement[])=>items.length?[el('div',{role:'group','aria-label':label,class:'menu-group'},el('div',{class:'menu-heading','aria-hidden':'true'},label),...items)]:[];
      panel.append(el('div',{class:'menu-list sched-tools',role:'menu','aria-label':'Rota tools'},
        ...section('Check',menuItem('Rules in this rota','rules',()=>{close();openRotaRules();},{},(w.rule_adjustments||[]).length?plural((w.rule_adjustments||[]).length,'change'):''),free('Who’s available','people',()=>openRotaRules('people')),...(fixSum&&(fixSum.must||fixSum.could)?[hold(menuItem('Fix list','check',()=>{close();openIssues(w.id);},{},reading?'':fixSum.must?`${fixSum.must} must fix`:`${fixSum.could} could improve`))]:[]),...(toResolve?[menuItem('Resolve problems one by one','target',()=>{close();resolveNow();},{},plural(toResolve,'step'))]:[]),
          // How many rules and targets this rota changes for itself shows beside each entry (the top bar has no rules chip).
          hold(menuItem(ROTA_TARGETS,'sliders',()=>{close();rotaTargetsDialog(w.id);},{},(w.target_overrides||[]).length?plural((w.target_overrides||[]).length,'change'):''))),
        // Changing a published rota makes an amendment: an edit, not sharing (SR-02).
        ...section('Edit',...(draft?[...(w.baseline?[item('Refresh draft baseline','reset',()=>void refreshDraftBaseline(w,refresh).catch(reportError))]:[]),item('Add recurring sessions','calendar',withDraft(seriesMaterialise)),item('Update recurring sessions','reset',withDraft(seriesFollowing)),
          ...(absenceIds.length&&absenceIds.length<=100?[item('Prepare cover for recorded absences','people',withDraft(d=>bulkImpactDialog(ctx(),d,absenceIds,'release_unavailable',()=>{})))]:[]),
          item('Exchange two assignments','swap',withDraft(d=>swapDialog(d)))]:[]),...(permits(CAPABILITY.COORDINATOR)?[item('Change a published rota…','edit',()=>void changePublished())]:[])),
        ...section('Share',...(draft&&w.visibility==='private'?[item('Share with schedulers…','eye',()=>shareDraft(w,refresh))]:[]),
          ...(!w.archived_at&&w.status==='draft'&&w.visibility==='shared'&&w.purpose!=='integrated'&&rotaCoordinatorIds.includes(me!.identity_id)?[item('Make this the rota…','calendar',()=>designateDraft(w,refresh))]:[]),
          ...publishedTools(free,false)),
        // Archive or Delete draft comes last on its own: the rota's own read (whole or compact) names it in
        // allowed_actions, so a rota past the list's first page has it too (rota_lifecycle.ts).
        ...((life=>life?[el('hr',{class:'menu-sep'}),life]:[])(w.archived_at?null:lifecycleItem(w,item,()=>{workspaceId='';return refresh();})))));
    },{label:'Rota tools',placement:'below-end',key:'rota-tools:'+w.id}),{class:'bare','aria-haspopup':'menu'});
    // A live update or check redraws the page: an open Rota tools menu stays open on the new button.
    queueMicrotask(()=>adoptPopover('rota-tools:'+w.id,tools!));
    // Draft edits save at once: their Undo is always one press away, not only in a toast or the tools menu.
    if(draft&&!w.archived_at){
      const b=iconButton('Undo last edit','undo',()=>undoLast(),{class:'bare','data-tip':'Undo (Ctrl Z)','aria-keyshortcuts':'Control+Z Meta+Z'});
      undoButton=reading?hold(b):w.can_undo===true?b:notYet(b,w.can_undo===false?NO_UNDO:'Undo availability is unknown');
    }
    // SW3-A-06: after your own Undo (the server's can_redo), Redo sits beside it until the rota changes again. H-11: from
    // the Undo's own reply at once, not only once the draft has been read again (a minute under load).
    if(draft&&((draftData as {can_redo?:boolean}|null)?.can_redo||redoAfter?.id===w.id&&redoAfter.revision===w.revision))redoButton=hold(redoBarButton());
  }else if(planner||permits(CAPABILITY.STAFF)){
    // The published rotas in view that this person may archive (allowed_actions), last and on their own.
    const pubs=work.filter(r=>r.status==='published'&&period.days.some(d=>r.starts_on<=d&&d<r.ends_on));
    const archivable=(item:(label:string,glyph:IconName,run:()=>unknown)=>HTMLElement)=>{const items=pubs.map(r=>lifecycleItem(r,item,()=>refresh(),pubs.length>1)).filter(Boolean) as HTMLElement[];
      return items.length?[el('hr',{class:'menu-sep'}),...items]:[];};
    // The published schedule's tools sit where a draft's Rota tools do, in the top bar (S-18), not in the toolbar.
    const list=publishedTools((label,glyph,run)=>menuItem(label,glyph,run));
    if(list.length){tools=iconButton('Schedule tools','dots',()=>popover(tools!,(panel,close)=>{panel.append(el('div',{class:'menu-list sched-tools',role:'menu','aria-label':'Schedule tools'},...publishedTools((label,glyph,run)=>menuItem(label,glyph,()=>{close();return run();})),...archivable((label,glyph,run)=>menuItem(label,glyph,()=>{close();return run();}))));},{label:'Schedule tools',placement:'below-end',key:'schedule-tools'}),{class:'bare','aria-haspopup':'menu'});
      queueMicrotask(()=>adoptPopover('schedule-tools',tools!));}
  }
  const planning=w?planningOf(whole):null;
  const bar=el('div',{class:'planner-bar review-bar'},planner?switcher:el('div',{class:'department-title page-title'},el('h1',{},'Schedule')),statusPill||'',driftButton||'',planning&&w?groupScopeButton(w.id,planning):'',changeChip||'',el('span',{class:'grow'}),help||'',undoButton||'',redoButton||'',tools||'',auto||'',workflow);  // the main action (workflow) last, next to the bell
  // A load that finishes after you have moved on must not take over the new page's top bar.
  if(mySerial!==renderSerial)return;
  swapPlannerBar(bar,target);
  // Fit after the swap so fitRow can release the outgoing toolbar's observers.
  fitRow(bar,['tight','tighter','tightest'],()=>phoneQuery.matches||[bar,...bar.querySelectorAll<HTMLElement>('.health-pills')].some(e=>e.scrollWidth>e.clientWidth+1));
  // The bar now names the new rota or view. A body still on screen from another rota or view goes to its skeleton at
  // once, toolbar included, so the two states never mix while this one loads (S-27); a live redraw of the same view
  // keeps what it shows.
  const stale=document.querySelector<HTMLElement>('#main .planner-body'),staleKey=stale?.querySelector<HTMLElement>('#schedule-view')?.dataset.viewKey;
  if(stale&&!target.contains(stale)&&staleKey!==undefined&&staleKey!==`${w?.id}|${plannerViewId}|${schedView}|${layerMode}`)
    stale.replaceChildren(skeleton(scheduleSkeleton(schedView),`Loading ${w?w.name:'the schedule'}…`,{fill:true}));

  // ---- Body: the view, the auto-schedule panel beside it, rules in context ----
  const bodyWrap=el('div',{class:'planner-body'+(autoChecking&&draft&&!fresh?' is-working':'')});const mainCol=el('div',{class:'planner-main'});bodyWrap.append(mainCol,side);
  let reloadScheduling:(()=>Promise<void>)|undefined;
  if(auto?.classList.contains('auto-button')&&!whole)auto.addEventListener('click',()=>{schedulingSettings.open=true;switchView('week');void redraw();});
  // The top bar is redrawn in place (a rota check landing): a press it replaces mid-way still acts (dom.ts footerAct).
  else if(auto?.classList.contains('auto-button'))footerAct(auto,'auto-schedule',()=>unpin(()=>{
    // A superseded bar hands the press to the current one; the current bar while its redraw is still being attached
    // (bar swapped in first, body after) toggles its own panel, which then attaches open or closed.
    if(!side.isConnected&&currentAuto()!==auto){currentAuto()?.click();return;}schedulingSettings.open=side.hidden;side.hidden=!schedulingSettings.open;auto!.setAttribute('aria-expanded',String(schedulingSettings.open));if(schedulingSettings.open){closeWeekPanel();side.querySelector<HTMLElement>('summary,button')?.focus({preventScroll:true});
    // Below 1180px the panel stacks under the schedule (CSS): bring it into view, or the button seems to do nothing.
    if(matchMedia('(max-width:1180px)').matches)side.scrollIntoView({block:'start'});void reloadScheduling?.().catch(e=>{reportError(e);});}}));
  target.append(bodyWrap);target=mainCol;
  if(w&&whole){const data=whole;
    // Rules in context: hover or pin anything on the schedule to see the rules that apply there, and highlight a rule.
    mountRules(ctx(),data,bodyWrap,mainCol,{editable:draft,canPropose:may('rule.propose'),canRequestException:permits(CAPABILITY.COORDINATOR),
      policy:id=>rows('policies').find(p=>p.id===id),sentence:ruleSentence,rotaChanges:()=>rotaTargetsDialog(w.id),refresh,
      openRule:(id,person)=>void openRuleSource(id,person).catch(reportError),
      fix:draft?focus=>openIssues(w.id,focus||{}):undefined,
      // Make it the department default: the rule's propose form, prefilled with this rota's number; a catalogue rule's
      // rota adjustment opens the server's seed in the line panel (promote-adjustment), else the rule's own page.
      changeForEveryone:(p,amount,adjustment)=>p.definition?.rule?.kind!=='catalogue'?ruleChangeForm(ctx(),p,undefined,amount)
        :adjustment?void promoteAdjustment(ctx(),p.id,w.id,adjustment).catch(reportError):void openRuleSource(p.id).catch(reportError),
      cover:(()=>{const c=coverOf(data);return (id:string)=>c.get(id);})(),
      // A person's details are the one person card (D67/D68): contacts with Copy and Call as each contact type allows.
      personDetails:pid=>{const p=rows('people').find(x=>x.id===pid);return p?personCard(ctx(),p):el('p',{class:'muted'},'This person is not in your lists.');},
      ownRules:()=>rows('policies').filter(p=>p.status==='proposed'&&p.definition?.workspace_id===w.id),
      addRotaRule:draft?()=>policyForm(ctx(),'',{workspace_id:w.id,title:w.name+' · ',starts_on:w.starts_on,ends_on:w.ends_on}):undefined,
      onOpen:()=>{closeWeekPanel();closeAuto();}});
    if(permits(CAPABILITY.COORDINATOR)){
      // An already-open panel must finish its first read before a same-page redraw swaps it in; otherwise the
      // breakpoint change briefly exposes only the loading skeleton and drops the step rail.
      const panelLoad=schedulingPanel(side,data,draft,!!memo,returnFromAuto);
      if(autoOpen)reloadScheduling=await panelLoad;
      else void panelLoad.then(r=>{reloadScheduling=r;});
    }
    // ME-26: the review strip says what an amendment changes and the coordinator's note, where the approver decides.
    if(w.review_snapshot&&['submitted','approved'].includes(w.status)){const strip=reviewPanel(ctx(),data);
      strip.append(...submittedFacts(data));target.append(strip);}
    if(w.urgency==='urgent')target.append(note('Urgent amendment · '+w.urgency_reason+' · Mandatory validation and independent review still apply.','warning'));
    // An approver's return stays on the draft (who, when, why) until it is submitted again.
    const back=w.status==='draft'?w.return_note:null;
    if(back)target.append(note(`Returned for changes by ${back.by_name||'an approver'} · ${formatDate(back.at,zone())} ${formatTime(back.at,zone())} · “${back.reason}”`,'warning'));
  }else side.hidden=true;
  if(source)target.append(note(`${source.snapshot.workspace.name} · version ${source.publication.sequence} is ${source.publication.current?'published and read-only.':'superseded and read-only. Staff work from the current version.'}`,source.publication.current?'info':'warning'));

  // Search in the People layout finds people; in every other layout it finds sessions.
  const filters:PlannerViewDefinition={schema_version:1,mode:'week',search:schedView==='people'||schedSearch?'':searchText,location_id:null,assignment_type_id:null,person_id:mineOnly&&me!.person_id?me!.person_id:null};  // the other filters: filterSessions
  const plannerSaved=await savedViews;
  plannerMemo={id:w?.id||'',offset:workspaceListOffset,page,views:plannerSaved};
  // A view chosen while this draw was reading (an Undo bringing a deleted view back) is kept for the draw it starts.
  if(plannerViewId&&plannerViewId===viewAtStart&&!plannerSaved.some(x=>x.id===plannerViewId))plannerViewId='';
  writeRoute();
  const activeView=plannerSaved.find(x=>x.id===plannerViewId);
  // Print › Range › This rota: the draft in view, else the published rota on the day in view.
  exportRota=(w||work.find(r=>r.status==='published'&&r.starts_on<=planDay&&planDay<r.ends_on)||null) as typeof exportRota;
  schedulePeople=()=>sctx().catalogues.people;  // the People view's people (a published layer adds its own)
  scheduleExport=()=>({title:activeView?.name||SCHED_VIEWS.find(v=>v.key===schedView)!.label,period:periodLabel(rangeNow(),period),department:me!.department.name,
    status:w?`${w.name} · ${stageName(w.status)}`:source?`Published version ${source.publication.sequence}${source.publication.current?'':' · superseded'}`:'Published',draft:!!w,by:me!.display_name,
    filters:filter?.active?.().map(f=>f.name?`${f.name}: ${f.value}`:f.value)||[],view:document.getElementById('schedule-view')!});
  const toolbarSlot=el('div',{class:'planner-toolbar'});
  target.append(toolbarSlot);
  // B7: a check that stopped says why, under the toolbar, with Check again (the results shown are the previous check's).
  // Also a failed job of a provisional check (its stored partial check still counts as current): never a silent stop.
  if(w&&draft&&(!fresh||serverChecking(w.validation))&&checkStop?.rota===w.id){const line=note('','error');line.classList.add('stale-banner','check-stopped');line.setAttribute('role','status');line.id=CHECK_STOPPED_WHY;
    line.querySelector(':scope>span')?.replaceChildren(el('strong',{},checkStop.lead),checkStop.cause?' '+checkStop.cause:'');
    line.prepend(icon('alert'));if(!checkStop.refused)line.append(button(CHECK_AGAIN,checkAgain,'secondary small'));target.append(line);}
  if(Object.entries({location_ids:'locations',assignment_type_ids:'assignment_types',person_ids:'people',category_ids:'categories',programme_ids:'programmes',service_ids:'services'}).some(([k,kind])=>(schedFilters[k]||[]).some(id=>!find(kind,id))))target.append(note('A saved filter refers to a record no longer available to this account. Clear or replace that filter; it has not been silently dropped.','warning'));
  const sel=selectionBar({id:'selection-bar',label:'Actions for selected sessions'}),selectionBarRoot=sel.root;
  // Only the actions that change something for the sessions picked (e.g. Unlock only when one is locked).
  selectionUpdate=()=>{autoRunSync?.();if(!selected.size||!whole){sel.show(null);return;}const data=whole;
    const picked=data.shifts.filter(s=>selected.has(s.id)),types=new Map(rows('assignment_types').map(t=>[t.id,t.revision]));
    const bulkRun=(action:'cancel'|'reopen'|'remove'|'refresh_types')=>()=>bulkImpactDialog(ctx(),data,[...selected],action,()=>selected.clear());
    // Held (sessionsHeld): Close, Reopen, Lock, Unlock and Remove stay in place, disabled, their tips saying why (guard_changes refuses them).
    const held=sessionsHeld(data),heldAct=(why:string,tip?:string)=>held?{disabled:true,tip:why,run:()=>notify(why,'info')}:tip?{tip}:{};
    // "Open" means a missing person elsewhere, so only closed and locked are counted here.
    const closed=picked.filter(s=>s.cancelled).length,locked=picked.filter(s=>s.locked).length;
    const lockAll=(on:boolean)=>()=>setLocked(data,picked.filter(s=>!!s.locked!==on&&!s.cancelled),on).then(()=>selected.clear());
    const away=picked.filter(s=>!period.days.includes(toLocal(s.start,zone()).slice(0,10))).length;
    // Resolve these (SI-16): their open places and blocking problems one by one, as Resolve does for the whole rota.
    const toResolve=resolveQueue(data,selected);
    sel.show({count:selected.size,noun:['session','sessions'],key:JSON.stringify([data.workspace.id,...picked.map(s=>s.id).sort()]),detail:[closed?`${closed} closed`:'',locked?`${locked} locked`:'',away?`${away} in other weeks`:''].filter(Boolean).join(' · ')||undefined,
      clear:()=>{selected.clear();renderPage();},actions:[
      ...(toResolve.length?[{label:'Resolve these',icon:'target' as IconName,run:()=>{startResolve(data.workspace.id,selected);schedulingSettings.open=schedulingSettings.editing=false;toCalendar(toResolve[0].shift);renderPage();},tip:plural(toResolve.length,'step')}]:[]),
      ...(picked.some(s=>!s.cancelled)?[{label:'Close sessions',icon:'close' as IconName,run:bulkRun('cancel'),...heldAct(onlyRotaCoordinator('close'),'Kept on the rota, marked closed')}]:[]),
      ...(picked.some(s=>s.cancelled)?[{label:'Reopen',icon:'play' as IconName,run:bulkRun('reopen'),...heldAct(onlyRotaCoordinator('close'))}]:[]),
      {label:'Copy',icon:'copy',run:()=>copySelected(data),tip:'Copy to other days or weeks'},
      ...(picked.some(s=>!s.locked&&!s.cancelled)?[{label:'Lock',icon:'lock' as IconName,run:lockAll(true),more:true,...heldAct(onlyRotaCoordinator('lock'),'Auto-schedule keeps locked sessions as they are')}]:[]),
      ...(picked.some(s=>s.locked)?[{label:'Unlock',icon:'lock' as IconName,run:lockAll(false),more:true,...heldAct(onlyRotaCoordinator('lock'))}]:[]),
      ...(picked.some(s=>types.has(s.assignment_type_id)&&types.get(s.assignment_type_id)!==s.type_revision)?[{label:'Refresh type versions',icon:'history' as IconName,run:bulkRun('refresh_types'),tip:'Use the current duty type settings',more:true}]:[]),
      {label:'Remove',icon:'trash',run:bulkRun('remove'),tip:'Delete these sessions from the draft',danger:true,...heldAct(onlyRotaCoordinator('remove'))}]});};
  if(draft)target.append(selectionBarRoot);selectionUpdate();
  const people=Object.fromEntries(sctx().catalogues.people.map(r=>[r.id,r])),locations=Object.fromEntries(sctx().catalogues.locations.map(r=>[r.id,r]));
  dragWorkspace=draft?whole:null;
  // A published session (or anything in the published-only schedule) opens read-only.
  // A session without the planner's panel beside it (a published session, or a draft's on the phone's day list): the
  // same summary docked, with the session's actions when it is a draft one this person may change (S-09, S-22).
  const sessionDock=(s:Row,acts?:{edit?:()=>void;more:MenuEntry[]})=>dockPanel({key:'session-details',title:`${formatDay(s.start,zone())} · ${s.type_snapshot.name}`,
    sub:`${formatTime(s.start,zone())}–${formatTime(s.end,zone())} · ${labelOf(locations[s.location_id])}${s.context||!draftData?' · Published':''}`,
    build:dock=>{const {body,close}=dock;
      // A published-only schedule's checks are the live check of what is published (publishedLive), current when read.
      const stored=!draftData?(shown.workspace.validation as unknown as Row|undefined):null;
      const live=!draftData&&publishedLive?.key===liveKey(layer)?publishedLive.evaluation as unknown as Row:null;
      const summary=(person?:(p:Row)=>Node|null)=>sessionSummary(sctx(),s,{validation:s.context?null:draftData?draftData.workspace.validation as Row|undefined:(live||stored),name:id=>fullNameOf(people[id]),code:id=>displayName(people[id]),refresh:async()=>{close();await refresh();},fresh:draftData?fresh:true,kept:reading,person});
      const pub=String(s.publication_id||source?.publication.id||'');
      const published=!acts&&(s.context||!draftData)&&!!pub&&changeable(s);
      if(pubHooks&&published)publishedSession(dock,s,pub,pubHooks,summary);else body.append(summary());
      // SW-05: the person's own published duty still ahead offers Offer swap and Can't make it, as Home does.
      const mine=published&&!pubHooks?(s.participants as Row[]).find(p=>p.person_id&&p.person_id===me!.person_id):null;
      const duty=mine?{...mine,source_id:pub,start:mine.start||s.start,end:mine.end||s.end}:null;
      const own=duty?dutyButtons(ctx(),duty,`${s.type_snapshot.name} · ${labelOf(locations[s.location_id])}`,undefined,reportedDuty(ctx(),duty)).filter(Boolean):[];
      if(own.length)body.append(el('footer',{class:'form-footer pc-foot'},own[0]!,el('span',{class:'grow'}),...own.slice(1)));
      if(acts)body.append(el('div',{class:'actions'},acts.edit?button('Edit session',acts.edit,'primary'):'',...acts.more.map(m=>button(m.label,()=>{close();m.run();},'secondary'))));
      if(acts&&sessionsHeld(draftData))body.append(el('p',{class:'hint sessions-held'},onlyRotaCoordinator('add')));}});
  const readOnly=(s:Row)=>sessionDock(s);
  readOnlySession=readOnly;
  // F7 (maintenance replace_action): ?shift=<published session>&replace=<assignment> opens that session straight on Replace.
  const replaceAsk=routeQuery.get('replace');
  // A first draw may come before the week's published sessions: the request waits for the next one, said once missed twice.
  const shiftAsk=routeQuery.get('shift')||routeShift;
  // G-05: on a draft (Home › Duties during approved leave), that session's replacement list for that person (the
  // assignment, or the person), their chip marked and focused.
  if(replaceAsk&&shiftAsk&&draft&&whole){const s=whole.shifts.find(x=>String(x.id)===shiftAsk),a=(s?.participants as Row[]|undefined)?.find(p=>p.id===replaceAsk||p.person_id===replaceAsk);
    routeQuery.delete('replace');if(s&&a){queueFill(w!.id,String(s.id),String(a.id),undefined,undefined,/* focusSearch */false);routePerson=String(a.person_id);}}
  else if(replaceAsk&&shiftAsk&&pubHooks){const s=pubHooks.sessions().find(x=>String(x.id)===shiftAsk);
    if(s||++replaceMisses>1){routeQuery.delete('replace');replaceMisses=0;}
    if(s&&changeable(s)){replaceNext(String(s.id),replaceAsk);queueMicrotask(()=>readOnly(s));}else if(!s&&!routeQuery.has('replace'))notify('That duty is not on this week of the published rota.','warning');}
  if(proposal&&proposal.workspace_id!==w?.id)proposal=null;
  if(proposal){const p=proposal;target.append(el('div',{class:'proposal-bar',role:'status'},icon('sparkle'),el('span',{},el('strong',{},'Showing the auto-schedule proposal'),` · ${p.additions.length} to add · ${p.removals.length} to remove · nothing is saved until you apply it`),
    el('div',{class:'actions'},button('Review and apply',()=>inspectSchedulingJob(p.job),'primary'),button('Dismiss',()=>{proposal=null;renderPage();},'ghost'))));}
  // An empty view's "Show all" (schedule.ts scheduleEmpty): the filters and the search go; the layers stay.
  const showAllSessions=()=>{schedFilters={};schedConditions=null;searchText='';schedSearch=undefined;weekView.onlyOpen=false;weekView.onlyIssues=false;void redraw();};
  // Only your own duties in view: My duties on, or a reader the server gives only their own (maintenance); Show everyone where
  // the reader may see the team's.
  const mine={mine:!!me!.person_id&&(mineOnly||!team),onShowEveryone:mineOnly&&team?()=>{mineOnly=false;void redraw();}:undefined,
    nextDuty:team?undefined:async()=>{const next=(await upcoming(ctx())).duties[0];if(!next)return null;const on=toLocal(next.start,zone()).slice(0,10);
      return {label:`Next duty: ${formatDay(on,'UTC')}`,go:()=>navigate(`schedule/week/${on}`)};}};
  const editing=draft&&!reading;
  const weekHooks:WeekHooks=draftData?{fixedHeld:sessionsHeld(draftData),sessionsHeld:editing&&sessionsHeld(draftData),open:reading?readOnly:undefined,kept:reading,name:displayName,code:shortCode,details:s=>{if(s.context)readOnly(s);else if(!openSession(s))sessionDock(s,weekHooks.sessionActions?.(s));},view:readOnly,dnd,proposal,editable:editing,fresh,selected,onSelect:updateSelectionBar,refresh,
    live:()=>heldDraft(draftData),lock:permits(CAPABILITY.COORDINATOR)?(s,on)=>setLocked(heldDraft(draftData),[s],on):undefined,
    fill:s=>{queueFill(w!.id,s.id);toCalendar(s);renderPage();},
    fixList:editing&&planOnDesk()?f=>{const l=fixCache.get(w!.id)?.list;if(f?.shift&&l&&!causeOfShift(l,f.shift))return false;openIssues(w!.id,f?.shift?{shift:f.shift}:{});return true;}:undefined,
    onResolveMove:s=>{toCalendar(s);renderPage();},
    autoSchedule:scope=>{schedulingSettings.open=true;if(scope)schedulingSettings.scope=scope;renderPage();},
    onPanel:show=>unpin(()=>{closeAuto();show();}),
    onPanelChange:id=>{if(routeShift||routeQuery.get('panel')!=='session'||routeQuery.get('edit')!==w!.id)return;
      if(id)routeQuery.set('shift',id);else{routeQuery.delete('shift');routeQuery.delete('panel');}writeRoute();},
    menu:reading?undefined:t=>t.shift?.context?[]:plannerShortcuts(heldDraft(draftData),t,draft),onTable:p=>{listParts=p;},exchange:editing?a=>swapDialog(heldDraft(draftData),a):undefined,
    create:editing?'Add session':undefined,onCreate:editing?()=>shiftForm(ctx(),draftData,undefined,shownDay(draftData)):undefined,onShowAll:()=>showAllSessions(),...mine,
    // The session panel's footer (S-09): Edit session first, then ⋯ (close or reopen, cover, staffing).
    sessionActions:s=>!reading&&draftData.workspace.status==='draft'&&permits(CAPABILITY.COORDINATOR)&&!s.locked?{edit:()=>shiftForm(ctx(),draftData,s),
      more:[{label:s.cancelled?'Reopen session':'Close session',icon:'close' as IconName,run:()=>sessionsHeld(draftData)?notify(onlyRotaCoordinator('close'),'info'):bulkImpactDialog(ctx(),draftData,[s.id],s.cancelled?'reopen':'cancel',()=>{})},
        ...(s.cancelled?[]:[{label:'Prepare automatic cover',icon:'people' as IconName,run:()=>bulkImpactDialog(ctx(),draftData,[s.id],'release_unavailable',()=>{})},
          {label:'Find staffing or replacement options',icon:'search' as IconName,run:()=>staffingDialog(ctx(),draftData,s)}])]}:undefined,
    why:{policy:id=>referencePolicy(id),open:(id,person)=>{void openRuleSource(id,person).catch(e=>reportError(e));},allowOnce:(rule,person,iso)=>exceptionButton(rule,person,iso,'Allow once…')}}
    :{name:displayName,code:shortCode,details:readOnly,open:readOnly,view:readOnly,dnd:node=>node,proposal:null,editable:false,fresh:true,selected:new Set(),onSelect:()=>{},refresh,onTable:p=>{listParts=p;},tallies:planner,
      create:planner&&permits(CAPABILITY.COORDINATOR)?'New rota':undefined,onCreate:planner&&permits(CAPABILITY.COORDINATOR)?()=>workspaceForm(ctx(),undefined,inView()):undefined,onShowAll:()=>showAllSessions(),unlinked:unlinkedStaff(me!),...mine};
  let listParts:Parameters<NonNullable<WeekHooks['onTable']>>[0]={};
  // Motion: a new view fades in; paging slides the way you moved. Plain re-renders (live updates) do not animate.
  const viewKey=`${w?.id}|${plannerViewId}|${schedView}|${layerMode}`,viewPanel=el('div',{id:'schedule-view',class:'schedule-view',role:'region','data-view-key':viewKey,'data-redraw-key':`${viewKey}|${range}`,'aria-label':`${SCHED_VIEWS.find(v=>v.key===schedView)!.label} view`});
  // The live check lands after its read, in a later redraw (loadLiveCoverage): this says which page has it drawn.
  viewPanel.toggleAttribute('data-live-checked',!!liveCheck);
  target.append(viewPanel);
  // The window on screen until the whole rota lands (wholeRota): read-only, its writes held; then the page draws it whole.
  // The week drawn from its window stays still (no dimming, no top bar) while the whole rota is read quietly; it is read,
  // paged and opened read-only meanwhile (D59: whole-rota work never runs on the window). Editing coming in is said once,
  // politely. A failed read says so above the week with Retry: never a week left quietly half-loaded.
  // The whole rota read ahead (readAhead) may already be in: then nothing is read again.
  if(reading){windowOnScreen=true;viewPanel.dataset.reading='';viewPanel.setAttribute('aria-label',viewPanel.getAttribute('aria-label')+' · from the last check');
    const id=w!.id,epoch=scheduleEpoch,here=()=>mySerial===renderSerial&&epoch===scheduleEpoch&&workspaceId===id&&view==='schedule';
    const readWhole=():void=>void (scheduleCache.drafts.has(id)?Promise.resolve():wholeRota(id,true).catch(e=>{throw rotaFailed(e);})).then(
      // Said once, politely, when the week can be edited: after its redraw, or at once when a page change is drawing it.
      ()=>{const ready=()=>{if(draft&&view==='schedule'&&workspaceId===id)say('Ready to edit');};if(here())void redraw().then(ready);else ready();},
      // A refusal or a rota gone (an ApiError rotaFailed keeps) is the page's to say, as before; a failed read is said here.
      e=>{if(sessionChanged(e)||!here())return;if(e instanceof ApiError){void redraw();return;}const failed=readFailed('the whole rota',e,()=>{failed.remove();readWhole();});viewPanel.before(failed);wholeBusy?.remove();});
    readWhole();}
  const nav=scheduleNav(rangeNow(),period,today,go,()=>planDay,viewDays.days,zone());
  let attention=-1,dayChanges=0;
  const phone=phoneQuery.matches;
  // Mine only (staff, or anyone with My duties on): the sessions they work; person facts still read the whole rota.
  // shortcut: the List view with a draft lists the draft's sessions only (rows are selectable for bulk draft actions).
  const {rota,unfiltered}=plannerSessions(shown.shifts,mineOnly&&me!.person_id||null,schedView==='list'&&!!draftData);
  const visibleData:WorkspaceData={...shown,shifts:searchSessions(filterSessions(unfiltered))};
  // Search › Show narrows only which people the renderers draw (weekView.personView); every shift keeps its whole
  // roster, so panels, checks, proposals and every command (remove, edit, assign, lead) read and write all of it.
  const about=pickedPeople(schedSearch,schedFilters,schedConditions);
  weekView.personView=schedView!=='people'&&about.length&&personDisplay!=='duty'?{people:about,display:personDisplay}:null;
  // SW3-A-31: the people a search names (picked, or whose name the text matches) are drawn first in their cells, so a
  // cell's "+N more" never hides the match. Order only: nobody is hidden for it.
  // A plain search (default options: schedSearch unset) names people too; fieldMatcher reads absent options as plain.
  const byText=schedView!=='people'&&!about.length&&searchText?fieldMatcher(searchText,normalSearch(schedSearch)):null;
  weekView.matchPeople=schedView==='people'?[]:about.length?about:byText?rows('people').filter(p=>byText(fullNameOf(p))).map(p=>String(p.id)):[];
  // An empty view tells the filters hiding everything (Show all) from a period with nothing in it (S-01).
  weekHooks.unfiltered=unfiltered;weekHooks.rota=rota;weekHooks.checking=autoChecking&&draft&&!fresh&&!checkStalled();
  // The check behind the latest edit has ended without a result (failed, not started, cancelled) or nobody runs it.
  weekHooks.checkEnded=()=>checkStop?.rota===w!.id||checkUnstarted.startsWith(w!.id+':')||!!checkCancelled&&checkCancelled===autoCheckKey&&autoCheckKey.startsWith(w!.id+':')||checkStalled();
  // An edit's reply: Submit, Approve and Publish are held at once (its check is queued), before the page redraws.
  weekHooks.edited=()=>{for(const b of workflow.children)if(b instanceof HTMLButtonElement&&b.getAttribute('aria-disabled')!=='true')notYet(b,waitingWord('queued'));};
  // Staff: a published duty inside their own recorded or approved leave says so ("Leave approved · cover being
  // arranged"); the publication itself is never changed by leave, so "Your duty" alone would contradict the approval.
  if(!draftData&&me!.person_id&&!permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)){
    const away=await myLeave().catch(()=>[] as Span[]);
    if(mySerial!==renderSerial)return;
    visibleData.shifts=visibleData.shifts.map(s=>s.participants.some(p=>p.person_id===me!.person_id)&&away.some(a=>a.start<Date.parse(s.end)&&a.end>Date.parse(s.start))?{...s,leave_pending:true}:s);
  }
  if(sideBySide&&draftData){
    viewPanel.append(nav);
    comparePanel(ctx(),draftData,viewPanel,compareId,id=>{compareId=id;writeRoute();});
  }else if(schedView==='year'){
    viewPanel.append(nav);const host=el('div',{class:'sd-host'},skeleton('calendar','Loading the year…'));viewPanel.append(host);
    try{attention=(await calendarDays(sctx(),host,{view:schedView,anchor:planDay,today,selected:pickedDay&&periodFor(rangeNow(),planDay,ws).days.includes(pickedDay)?pickedDay:planDay,
      drafts:[w?.id,layerMode==='side'?compareId:''].filter(Boolean) as string[],editId:w?.id||'',
      read:path=>{if(!(memo&&scheduleCache.windows.has(path))){const r=get(path).catch(e=>{scheduleCache.windows.delete(path);throw e;});scheduleCache.windows.set(path,r);}return scheduleCache.windows.get(path)!;},
      pick:day=>{pickedDay=day;},open:(day,v,draftId)=>{planDay=day;switchView(v);plannerViewId='';if(draftId&&draftId!==workspaceId&&drafts.some(d=>d.id===draftId)){workspaceId=draftId;draftNone=false;}void redraw();}})).attention;}
    catch(e){if(sessionChanged(e))throw e;host.replaceChildren(readFailed('the calendar',e,()=>void redraw()));}
  }else if(failed&&!draftData){
    const shown=readFailed('the published sessions',cause??new Error(failed),()=>void redraw());  // Retry in the message, as on every page
    viewPanel.append(nav,el('div',{class:'sd-host'},shown));
  }else if(useGrid()){
    const covering=schedView==='coverage',rows=covering?coverRows:rowsNow();
    const body=scheduleGrid(sctx(),visibleData,applyFilters(visibleData.shifts),period.days,{rows:rows==='duty'?'type':rows,columns:covering?coverColumns:gridColumns,group:covering?'none':groupNow(),subgroup:covering?'none':gridSubgroup,cells:!covering&&cellsNow()==='codes'?'codes':'counts'},viewDays.week_start??ws,weekHooks,viewDays.time_window);
    viewPanel.append(draftData?plannerPanels(sctx(),visibleData,period,weekHooks,nav,el('div',{class:'wk-layout'},el('div',{class:'wk-content'},body))).root:el('div',{class:'wk'},nav,body));
  }else if(schedView==='day'&&!phone){
    const drawn=dayTimeline(sctx(),visibleData,applyFilters(visibleData.shifts),period,()=>{},weekHooks,readOnly,nav,viewDays.time_window);dayChanges=drawn.changes;
    viewPanel.append(draftData?plannerPanels(sctx(),visibleData,period,weekHooks,nav,el('div',{class:'wk-layout'},el('div',{class:'wk-content'},drawn.root))).root:drawn.root);
  }else{
    if(weekView.groupBy!=='none'&&!SESSION_GROUPS.includes(weekView.groupBy))weekView.groupBy='service';  // the Week grid groups sessions only
    weekView.thenBy=schedView==='week'?gridSubgroup:'none';  // Then by: the one saved subgroup, on the Week's cards too
    if(schedView==='people'&&peopleView.leave){
      const band=await academicBand(period.days);
      // A newer render started while the band loaded (another layout or range): this one is stale; drawing it would
      // take the open panel's highlight away from the schedule on screen.
      if(mySerial!==renderSerial)return;
      viewPanel.append(...band);
    }
    const [layout,range]=({week:['week','week'],month:['week','month'],day:['week','day'],people:['person','2weeks'],coverage:['coverage','week'],list:['list','month']} as Record<string,[string,PlanRange]>)[schedView];
    planMode=layout;planRange=range;
    scheduleView(sctx(),viewPanel,visibleData,filters,layout,range,0,p=>go(stepAnchor(rangeNow(),planDay,p,viewDays.days)),weekHooks,period,Object.values(schedFilters).some(v=>v.length)||!!schedConditions||!!schedSearch&&(!!searchText.trim()||!!schedSearch.reference));
    // The renderer's own period strip is replaced by the one navigator (with Today).
    const own=[...viewPanel.querySelectorAll<HTMLElement>('.wk-nav')].at(-1);if(own)own.replaceWith(nav);else viewPanel.prepend(nav);
  }
  if(pubHooks)markChanges(viewPanel,pubHooks);
  if(draftData)markDiff(viewPanel,(draftData as {change_diff?:ChangeDiff|null}).change_diff);
  const calendarLayout=viewPanel.querySelector<HTMLElement>(':scope>.pv-range');
  const calendarGrid=['week','day','month'].includes(schedView)&&(!phone||weekLayoutNow()==='grid');
  const opt=(key:string,label:string)=>({key,label});
  // Each level's own order (group_sort) and Collapse all / Expand all over the heads drawn (the toolbar's Group).
  const groupOrder={value:()=>weekView.groupSort,set:(k:SortKey[])=>{weekView.groupSort=k;void redraw();}};
  const groupFolds={all:()=>{for(const g of shownGroups)collapsed.add(g);void redraw();},none:()=>{collapsed.clear();void redraw();}};
  // The Schedule grid groups by any dimension that is not its rows or columns, then by a second one (Then by).
  const grid=useGrid(),taken:string[]=[rowsNow()==='duty'?'type':rowsNow(),gridColumns];
  const gridGroup:ToolbarSpec['group']={options:schedView==='people'?[opt('rank','Rank'),opt('tag','Tag')]:DIMENSIONS.filter(d=>!taken.includes(d.key)).map(d=>opt(d.key,d.label)),
    value:()=>{const g=groupNow();return g==='none'?'':g;},defaultKey:schedView==='people'?'rank':'service',
    set:k=>{if(schedView==='people')peopleView.group=(k||'none') as typeof peopleView.group;else weekView.groupBy=(k||'none') as Dim|'none';if(!k||k===gridSubgroup)gridSubgroup='none';void redraw();},
    active:()=>groupNow()!==(schedView==='people'?'rank':'service')||gridSubgroup!=='none'||weekView.groupSort.length>0,
    then:{value:()=>gridSubgroup==='none'?'':gridSubgroup,set:k=>{gridSubgroup=(k||'none') as Dim|'none';void redraw();}},
    ...(schedView==='people'?{}:{order:groupOrder,collapse:groupFolds,
      levels:(keys:string[],order:SortKey[])=>{weekView.groupBy=(keys[0]||'none') as Dim|'none';gridSubgroup=(keys[1]||'none') as Dim|'none';weekView.groupSort=order;void redraw();}})};
  const group:ToolbarSpec['group']=schedView==='list'?listParts.group:grid?gridGroup:schedView==='people'?{options:PEOPLE_GROUPS,value:()=>peopleView.group==='none'?'':peopleView.group,set:k=>{peopleView.group=(k||'none') as typeof peopleView.group;refreshPeople();},active:()=>peopleView.group!=='rank'}
    :calendarGrid?{options:SESSION_GROUPS.map(k=>opt(k,dimOf(k).label)),value:()=>weekView.groupBy==='none'?'':weekView.groupBy,defaultKey:'service',
      set:k=>{weekView.groupBy=(k||'none') as typeof weekView.groupBy;if(!k||k===gridSubgroup)gridSubgroup='none';void redraw();},active:()=>weekView.groupBy!=='service'||schedView==='week'&&isSessionGroup(gridSubgroup),
      // Then by (one nested level of session dimensions) on the Week's session cards; Month and Day keep one level.
      ...(schedView==='week'?{then:{value:()=>isSessionGroup(gridSubgroup)?gridSubgroup:'',set:(k:string)=>{gridSubgroup=(k||'none') as Dim|'none';void redraw();}},collapse:groupFolds}:{}),
      levels:(keys:string[],order:SortKey[])=>{weekView.groupBy=(keys[0]||'none') as Dim|'none';if(schedView==='week')gridSubgroup=(keys[1]||'none') as Dim|'none';else if(keys[0]===gridSubgroup)gridSubgroup='none';weekView.groupSort=order;void redraw();},
      order:groupOrder}:undefined;
  const sort:ToolbarSpec['sort']=schedView==='list'?listParts.sort:schedView==='people'&&!grid?{options:PEOPLE_SORTS,value:()=>peopleView.sort.startsWith('name')?['name',peopleView.sort==='name-desc'?'desc':'asc']:['hours',peopleView.sort==='hours-asc'?'asc':'desc'],set:v=>{peopleView.sort=!v?'hours-desc':v[0]==='name'?(v[1]==='desc'?'name-desc':'name'):v[1]==='asc'?'hours-asc':'hours-desc';refreshPeople();},active:()=>peopleView.sort!=='hours-desc'}
    :grid&&rowsNow()==='person'?{options:[opt('hours','Hours, most first'),opt('code','Short code')],defaultLabel:'Name',value:()=>['hours','code'].includes(weekView.sortBy)?[weekView.sortBy,'asc']:null,set:v=>{weekView.sortBy=(v?.[0]||'time') as typeof weekView.sortBy;void redraw();},directions:false}
    :['week','month'].includes(schedView)&&calendarGrid?{options:[opt('type','Duty type'),opt('place','Place')],value:()=>['type','place'].includes(weekView.sortBy)?[weekView.sortBy,'asc']:null,set:v=>{weekView.sortBy=(v?.[0]||'time') as typeof weekView.sortBy;void redraw();},directions:false}:undefined;
  const refilter=()=>{void redraw();};
  const fields=scheduleFilterFields();
  // The shared Filter (condition builder, And/Or, is not, groups) over the Schedule's properties; "Only open places" and
  // "Only rule breaks" stay as two quick switches above it (SR-06).
  const onlys:[string,'onlyOpen'|'onlyIssues',string][]=[['Only sessions with open places','onlyOpen','Open places'],['Only sessions that break a rule','onlyIssues','Breaks a rule']];
  const filter:ToolbarSpec['filter']=schedView==='year'||!planner?undefined:conditionFilter({
    fields:()=>fields.map(f=>({key:f.key,label:f.label,choices:f.choices})),
    get:()=>({filters:schedFilters,conditions:schedConditions}),set:next=>{schedFilters=next.filters;schedConditions=next.conditions;refilter();},
    label:k=>fields.find(f=>f.key===k)?.label||k,valueLabel:(k,v)=>fields.find(f=>f.key===k)?.choices.find(c=>c.value===v)?.label||'Unavailable',
    before:(panel,changed)=>{if(w)for(const [label,key] of onlys){const c=check(label,weekView[key]);c.input.addEventListener('change',()=>{weekView[key]=c.input.checked;changed();refilter();});panel.append(c.wrap);}
      // Filtered by person: show them alone, with their recorded team, or everyone on their duties (as the search's Show).
      if(personPicked(schedSearch))panel.append(...segment('Show',Object.entries(PLANNER_PERSON_DISPLAYS) as [string,string][],personDisplay,k=>{personDisplay=k as typeof personDisplay;refilter();}));},
    extra:()=>{const on=onlys.filter(([,key])=>weekView[key]);return {count:on.length,clear:()=>{weekView.onlyOpen=false;weekView.onlyIssues=false;},
      active:on.map(([,key,value])=>({name:'Only',value,remove:()=>{weekView[key]=false;refilter();}}))};}});
  // The view's name for the View settings button: the built-in view, its range when it differs, and the drafts layer.
  const viewLabel=SCHED_VIEWS.find(v=>v.key===schedView)!.label+(viewRange?' · '+RANGE_NAMES.find(r=>r[0]===viewRange)![1]:'')+(w&&layerMode!=='edit'?` · ${layerMode==='side'?'Side by side':'Overlay'}`:'');
  const views={key:'planner',noun:'sessions',saved:plannerSaved,activeId:plannerViewId,shareable:permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN),current:plannerDefinition,dirty:(d:PlannerSaved)=>!sameView(d.planner,plannerDefinition().planner)||JSON.stringify(d.conditions||null)!==JSON.stringify(schedConditions)||!sameColumns('planner-list',d),
    // The menu reads these when it opens: a view just saved, renamed or deleted counts before the redraw it starts ends.
    done:(next:SavedView<PlannerSaved>[],id:string)=>{views.saved=next;views.activeId=id;if(plannerMemo)plannerMemo.views=next;plannerViewId=id;
      // The address names the view at once (a reload before the redraw ends opens it, collapsed groups included: SI-35).
      writeRoute();void redraw();}};
  const resetView=()=>{applyPlannerView(undefined);setTableColumns('planner-list',undefined);void redraw();};
  const sample=sctx().catalogues.people.find(p=>p.id===me!.person_id)||sctx().catalogues.people[0];
  // The View settings spec, also opened by Print or export in display-only mode (exportView).
  // Colour by, Density and Trainees per team are drawn by the Week and Month card grid only (Day's bars are coloured by
  // duty type and keep one size): offered only there, never as a setting that does nothing.
  const cardGrid=calendarGrid&&schedView!=='day';
  // The kind of item drawn (schedule_item.ts) and whether it is one person's duty (pills and grid chips by person).
  // A chip is one person's duty when any axis is a person's (rows, columns, groups); a group already names its dimension.
  const surface=surfaceNow(),personAxis=(k:string)=>!!DIMENSIONS.find(d=>d.key===k)?.person;
  const personItems=surface==='card'||surface==='pill'||surface==='chip'&&(schedView==='people'||[rowsNow(),gridColumns,groupNow(),gridSubgroup].some(personAxis));
  const headNames=(k:string)=>(surface==='card'||surface==='pill')&&[weekView.groupBy,schedView==='week'?gridSubgroup:''].includes(k==='duty'?'type':k);
  const settingsSpec=(close:()=>void):ViewSettingsSpec=>{const a=viewActions<PlannerSaved>(views);return {
    name:activeView?activeView.name:null,rename:name=>void a.rename?.(name),
    // SW3-C-18: a reader who plans nothing sees their own duties: no Coverage, no people rows, no planning marks.
    layout:()=>layoutOf(schedView),layouts:planner?LAYOUT_NAMES:LAYOUT_NAMES.filter(([k])=>k!=='coverage'),layoutHint:k=>LAYOUT_HINTS[k]||'',setLayout:k=>{setLayoutRange(k,rangeNow());void redraw();},
    range:()=>schedView==='year'?'':rangeNow(),ranges:RANGE_NAMES,custom:{value:()=>viewDays.days,set:n=>{viewDays.days=n;void redraw();}},
    days:{weekdays:()=>viewDays.weekdays,setWeekdays:d=>{viewDays.weekdays=d;void redraw();},weekStart:()=>viewDays.week_start,departmentStart:ws,setWeekStart:d=>{viewDays.week_start=d;void redraw();},
      window:()=>viewDays.time_window,setWindow:x=>{viewDays.time_window=x;void redraw();}},setRange:k=>{setLayoutRange(schedView==='year'?'calendar':rendererOf(schedView),k as SchedRange);void redraw();},
    // Coverage: rows of a session dimension, columns of a date axis or another session dimension (places belong to sessions).
    coverage:{rows:()=>coverRows,setRows:k=>{coverRows=k as Dim;void redraw();},columns:()=>coverColumns,setColumns:k=>{coverColumns=k as GridColumn;void redraw();},
      rowOptions:()=>DIMENSIONS.filter(d=>!d.person).map(d=>({value:d.key,label:d.many[0].toUpperCase()+d.many.slice(1),...(d.key===coverColumns?{disabled:true,hint:'Already the columns'}:{})})),
      columnOptions:()=>[...TIME_AXES.map(([value,label])=>({value,label})),...DIMENSIONS.filter(d=>!d.person).map(d=>({value:d.key,label:d.many[0].toUpperCase()+d.many.slice(1),group:'By category',
        ...(d.key===coverRows?{disabled:true,hint:'Already the rows'}:{})}))]},
    rows:{value:rowsNow,
      // Plural nouns read as the rows ("Rows: Services"); a dimension already the columns cannot also be the rows.
      options:()=>[{value:'duty',label:'Duties'},...DIMENSIONS.filter(d=>planner||!d.person).map(d=>({value:d.key,label:d.many[0].toUpperCase()+d.many.slice(1),...(d.key===gridColumns?{disabled:true,hint:'Already the columns'}:{})}))],
      set:r=>{if(r==='duty')setLayoutRange('calendar',rangeNow());
        else{gridRows=r as Dim;setLayoutRange(r==='person'&&peopleCells==='cards'&&gridCells!=='counts'?'people':'timeline',rangeNow());}
        if(weekView.groupBy===r)weekView.groupBy='none';if(gridSubgroup===r)gridSubgroup='none';void redraw();},
      // Cards need day columns; duty rows over weeks, weekdays or a category show counts only.
      cells:()=>{const c=cellsNow();return gridColumns!=='day'&&c==='cards'?'counts':c;},
      cellOptions:()=>{const r=rowsNow(),day=gridColumns==='day';
        return r==='duty'?(day?[['cards','Sessions'],['counts','Counts']]:[]):[['codes','Duty codes'],...(r==='person'&&day?[['cards','Duties and hours']] as [string,string][]:[]),['counts','Counts']];},
      setCells:c=>{gridCells=c as typeof gridCells;const r=rowsNow();
        if(r==='person'){if(c!=='counts')peopleCells=c as typeof peopleCells;gridRows='person';setLayoutRange(c==='cards'?'people':'timeline',rangeNow());}void redraw();},
      columns:()=>gridColumns,
      columnOptions:()=>[...TIME_AXES.map(([value,label])=>({value,label})),...DIMENSIONS.filter(d=>planner||!d.person).map(d=>({value:d.key,label:d.many[0].toUpperCase()+d.many.slice(1),group:'By category',
        ...(d.key===(rowsNow()==='duty'?'type':rowsNow())?{disabled:true,hint:'Already the rows'}:{})}))],
      setColumns:c=>{gridColumns=c as GridColumn;if(weekView.groupBy===c)weekView.groupBy='none';if(gridSubgroup===c)gridSubgroup='none';void redraw();}},
    openYear:schedView==='year'?undefined:()=>{switchView('year');void redraw();},
    // Colour by is one rule (colourRule) for every item: a session's hue by duty type or service; a person's duty also by
    // person, rank or tag (tags need the department's tag records, which staff do not hold).
    // Coverage's table is counts by duty type; only its popover lists sessions, so Colour by is not offered there.
    colour:surface&&schedView!=='coverage'?{options:(Object.entries(PLANNER_COLOUR_LABELS) as [string,string][]).filter(([k])=>personItems||k==='type'||k==='service').filter(([k])=>k!=='tag'||!!personTagChoices().length||weekView.colourBy==='tag'),
      value:()=>!personItems&&!['type','service'].includes(weekView.colourBy)?'type':weekView.colourBy,set:k=>{weekView.colourBy=k as typeof weekView.colourBy;void redraw();},
      tags:{options:personTagChoices,value:()=>weekView.colourTags,set:ids=>{weekView.colourTags=ids;void redraw();},limit:PLANNER_COLOUR_TAG_LIMIT}}:undefined,
    density:cardGrid?{value:()=>weekView.compact?'compact':'comfortable',set:k=>{weekView.compact=k==='compact';void redraw();}}:undefined,
    teamRows:cardGrid?{value:()=>weekView.teamRows,set:n=>{weekView.teamRows=n;void redraw();}}:undefined,
    // Planners' grids that draw staffing numbers (Week and Month day heads and group totals, the grid's Staffed / needed row).
    staffing:planner&&(cardGrid||schedView==='timeline')?{value:()=>weekView.staffing,set:on=>{weekView.staffing=on;void redraw();}}:undefined,
    // A layout offers what its items draw (ITEM_DRAWS, the one item renderer): People only where an item is a session,
    // Leave and wishes on a grid's person days (People × Days keeps its Layers switch), tags on a person's duty.
    properties:{value:()=>propertiesOf(weekView.props,surface||'card'),set:p=>{weekView.props=p;void redraw();},
      // Coverage: its popover's session items (no day heads there, so no rule marks).
      drawn:k=>k==='label'||!!surface&&ITEM_DRAWS[surface].has(k)&&!headNames(k)&&!(schedView==='coverage'&&k==='rule')&&(k!=='people'||surface!=='chip'||schedView==='timeline'&&!personItems)
        &&(k!=='leave'||schedView==='timeline'&&rowsNow()==='person'&&gridColumns==='day')&&(!['tags','lead','overtime'].includes(k)||surface!=='chip'||personItems)&&(k!=='tags'||personItems),
      supports:k=>(planner||!['open','rule','places'].includes(k))&&(k!=='tags'||!!personTagChoices().length)},
    // Your own published duties (staff, or My duties without a draft): chips keep their time · place label (planner_week S-12).
    personal:!draftData&&(mineOnly||!planner),
    // List hides its table toolbar: a hidden column comes back here, through the table's own Shown properties.
    columns:schedView==='list'&&listParts.view?.build?{build:(b,changed)=>listParts.view!.build!(b,changed,close)}:undefined,
    label:{value:labelNow,set:l=>{viewNames=l;void redraw();},preview:l=>sample?displayName(sample,l):'',part:(p,l)=>sample?labelPart(sample,p,l.honorific,labelFacts(sample)):''},
    layers:{hint:()=>['Published',w?w.name:'',peopleView.leave&&planner&&schedView==='people'?'leave':'',mineOnly?'my duties':''].filter(Boolean).join(', '),build:panel=>{
      // Published always; one draft to edit, optionally a second side by side or the published days under it.
      panel.append(el('div',{class:'sd-layer'},el('span',{class:'sd-dot key','aria-hidden':'true'}),el('span',{class:'issue-text'},el('strong',{},'Published'),el('small',{},planner?'Always shown, read-only':'Your published duties')),icon('lock')));
      if(planner){
        const name='layer-draft-'+renderSerial;
        for(const r of [...drafts,null]){const input=el('input',{type:'radio',name,checked:r?r.id===w?.id:!w}) as HTMLInputElement;input.addEventListener('change',()=>chooseDraft(r?.id||''));
          panel.append(el('label',{class:'sd-layer'},input,el('span',{class:'issue-text'},el('strong',{},r?r.name:'No draft'),el('small',{},r?`${stageName(r.status)} · ${formatDate(r.starts_on,zone())} – ${formatDate(lastIncludedDate(r.ends_on),zone())}`:'Published only')),r?el('span',{class:'sd-dot key draft','aria-hidden':'true'}):''));}
        if(w)panel.append(...segment('Show drafts',[['edit','Edit one'],['side','Side by side'],['overlay','Overlay']],layerMode,k=>{layerMode=k as LayerMode;void redraw();}));
        // People × Days draws leave and wishes as a layer (on by default); the grid's person rows have the property instead.
        if(schedView==='people'){const leave=check('Leave and wishes',peopleView.leave);leave.input.addEventListener('change',()=>{peopleView.leave=leave.input.checked;void redraw();});
          panel.append(el('div',{class:'sd-layer'},leave.wrap));}
        if(me!.person_id){const m=check('My duties',mineOnly);m.input.addEventListener('change',()=>{mineOnly=m.input.checked;void redraw();});panel.append(el('div',{class:'sd-layer'},m.wrap));}
      }}},
    saved:()=>!activeView||activeView.can_edit===false?'none':views.dirty(activeView.definition)?'dirty':'clean',saveAs:a.saveAs,update:()=>a.update?.()||Promise.resolve(),duplicate:a.duplicate||undefined,reset:resetView,
    sync:()=>tbar.sync()};};
  const settings=(panel:HTMLElement,_changed:()=>void,close:()=>void)=>viewSettings(settingsSpec(close),panel,close);
  scheduleSettings=settingsSpec;
  const tbar=collectionToolbar({label:'Schedule',key:'schedule',scope:[nav,calendarLayout,el('span',{class:'grow'})],
    // One box, named by what it finds in this view (SR-07): people in People, sessions elsewhere; Year has none (SR-10).
    search:{value:schedView==='year'?'':schedView==='people'?peopleView.find:searchText,label:schedView==='people'?'Search people':'Search sessions',placeholder:schedView==='year'?'Not in Year':schedView==='people'?'Person or code':'Name, code, duty or place',wait:schedView==='people'?150:300,
      onInput:q=>{if(schedView==='people')peopleView.find=q;else searchText=q;},apply:()=>{if(schedView==='people')refreshPeople();else refilter();},
      // Sessions only (People finds a person by name or code): records others can pick by name are for planners, who may read them.
      options:schedView==='people'?undefined:{value:()=>schedSearch,set:o=>{schedSearch=o;},can:{...serverCapability('sessions')!,...(planner?{}:{kinds:[]})},
        // A picked person: show them alone, with their recorded supervision team, or everyone on their duties.
        show:{label:'Show',options:Object.entries(PLANNER_PERSON_DISPLAYS).map(([key,label])=>({key,label})),value:()=>personDisplay,defaultKey:PLANNER_PERSON_DISPLAY_DEFAULT,
          set:k=>{personDisplay=k as typeof personDisplay;refilter();},when:personPicked}}},
    filter,group,sort,
    view:{label:()=>viewLabel,active:()=>false,tip:'View settings (layout, range, properties, colour, layers)',build:settings},
    saved:{name:()=>activeView?.name||'',dirty:()=>!!activeView&&views.dirty(activeView.definition),
      items:close=>savedViewItems<PlannerSaved>({...views,close,reset:resetView,apply:x=>{applyPlannerView(x?.definition.planner,x?.definition.conditions);setTableColumns('planner-list',x?.definition);plannerViewId=x?.id||'';void redraw();}})},
    // One primary (the teal one, S-08): Add session while a draft is open, New rota otherwise.
    // maintenance: no "My duties" look-alike toggle for readers who cannot switch it (a dead control); their empty view says it.
    actions:[draft&&!sideBySide?withIcon(button('Add session',()=>shiftForm(ctx(),draftData!,undefined,shownDay(draftData!)),'primary add-session',{'aria-label':'Add session','data-tip':sessionsHeld(draftData)?onlyRotaCoordinator('add'):'Add session',...(sessionsHeld(draftData)?{'aria-disabled':'true'}:{})}),'plus'):!w&&planner&&permits(CAPABILITY.COORDINATOR)?withIcon(button('New rota',()=>workspaceForm(ctx(),undefined,inView()),'primary add-session',{'aria-label':'New rota','data-tip':'New rota'}),'plus'):null]});
  tbar.root.classList.add('planner-toolbar','planner-options');toolbarSlot.replaceWith(tbar.root);
  // A grid's resized row headings, days or cells belong to the view: its saved-view button shows the change.
  scheduleSync=()=>tbar.sync();
  // Year draws day summaries: search and filters stay in their place, switched off, instead of vanishing (SR-10).
  if(schedView==='year'&&tbar.search){tbar.search.disabled=true;tbar.search.setAttribute('aria-label','Search sessions (not in Year)');tbar.sync();}
  if(reading)for(const n of tbar.root.querySelectorAll<HTMLElement>('.dt-search-wrap :is(input,button),.dt-tool[aria-label^="Filter"],.add-session'))hold(n);
  const motion=viewKey!==lastView.key?'pv-enter':planDay>lastView.day?'pv-next':planDay<lastView.day?'pv-prev':'';
  if(motion)[...viewPanel.children].forEach(n=>n.classList.add(motion));
  Object.assign(lastView,{key:viewKey,day:planDay});
  if(w&&whole){const data=whole;
    // Workload targets live in their dock (Rota tools); built only when it opens (its saved views are read then).
    plannerTargets=()=>{
      const targetsBox=el('div',{class:'targets-body'});
      if(w.validation?.quota_report)targetsBox.append(quotaPanel(w.validation.quota_report,fresh,'quotas-rota'));
      if(draft&&w.target_overrides?.length)targetsBox.append(el('section',{class:'panel'},el('h3',{},'Saved target adjustments'),...w.target_overrides.map((r:Row)=>el('div',{class:'actions'},el('span',{},name('people',r.person_id)+' · '+name('policies',r.policy_id)+' · '+r.amount+(r.reason?` · “${r.reason}”`:'')+(r.by_name?` · ${r.by_name}`:'')),button('Remove adjustment',()=>confirmAction('Remove target adjustment','Use the current rule calculation. If the rule no longer applies, its adjustment is simply removed.',async reason=>{await command('workspace.target_override',{policy_id:r.policy_id,person_id:r.person_id,amount:null,reason},w.id,w.revision);await refresh();},'Remove adjustment'),'secondary')))));
      if(draft&&!fresh)targetsBox.append(note('Workload targets update when the rota check finishes.','info'));
      if(draft&&fresh&&w.validation?.quota_report?.rows.some((r:Row)=>r.target_calculation))targetsBox.append(button('Adjust workload target',()=>formDialog('Adjust workload target','Changes apply only to this rota and require its independent approval.',form=>{
        const entries=w.validation!.quota_report!.rows.filter((r:Row)=>r.target_calculation);
        const selectedTarget=select('Employee and target',entries.map((r:Row,i:number)=>({value:String(i),label:r.display_name+' · '+r.policy_name})),'0');
        const targetStep=(metric:Row['metric'])=>metric==='duties'||metric==='lead_turns'?'0.01':'any';
        const amount=field('Override target','number',entries[0].amount,{required:true,min:0,max:100000,step:targetStep(entries[0].metric)}),reason=area('Adjustment reason','');reason.input.required=true;reason.input.minLength=3;
        const reset=check('Use calculated default again',false);reset.input.disabled=entries[0].target_calculation.override_amount===undefined;
        selectedTarget.input.addEventListener('change',()=>{const r=entries[Number(selectedTarget.input.value)];amount.input.value=String(r.amount);amount.input.step=targetStep(r.metric);reset.input.checked=false;reset.input.disabled=r.target_calculation.override_amount===undefined;amount.input.disabled=false;});
        reset.input.addEventListener('change',()=>{amount.input.disabled=reset.input.checked;});
        form.append(selectedTarget.wrap,amount.wrap,reset.wrap,reason.wrap);
        return async()=>{const r=entries[Number(selectedTarget.input.value)];await command('workspace.target_override',{policy_id:r.policy_id,person_id:r.person_id,amount:reset.input.checked?null:Number(amount.input.value),reason:reason.input.value},w.id,w.revision);await refresh();};
      },'Save target adjustment'),'secondary'));
      return targetsBox.childElementCount?targetsBox:null;};
    // Refresh only the same rota's open sheet, using this render's data. An older deferred callback never replaces
    // the current panel; expanded controls and typed edits stay in place while workload results update.
    const reopenTargets=()=>{if(mySerial===renderSerial&&view==='schedule'&&workspaceId===w.id)rotaTargetsDialog(w.id,true);};
    if(document.querySelector('[data-rota-targets]')){const other=document.querySelector('dialog[open]');if(other)other.addEventListener('close',reopenTargets,{once:true});else reopenTargets();}
    void data;
  }
  // Footer, left: the health of what is in view. With a draft: its open places, blocking, overlaps and score (where
  // you act). Published only: the published days in view (planners), or your duties (staff).
  // One definition for drafts and the published layer: the sessions in the period on screen (a draft holds all its days).
  // "In view" counts what the search shows too (People searches people, not sessions): an empty search result is 0, not the period's total.
  // SW3-A-30: the unit follows what is counted (the team's sessions, or your own duties), not the device.
  const inPeriod=new Set(period.days),live=(schedView==='people'?visibleData.shifts:applyFilters(visibleData.shifts)).filter(s=>!isClosed(s)&&!s.context&&inPeriod.has(toLocal(s.start,zone()).slice(0,10)));
  // Nothing published in view: no health to report (an empty schedule is not "Not checked yet").
  // One source in every view: the publications' stored check for the sessions in view (as Year and Month read it), never
  // the live re-check the grid marks use, so Week, Month and List agree.
  const noDraft=footerIsPublished(w);
  const shownPublished=noDraft&&planner&&layer?.shifts.length&&schedView!=='year'?layered(null,layer,layerMode,period,zoneNow):null;
  const absent=layer?absentOnDuty(layer,{shifts:live}):[];
  const publishedHealth=shownPublished&&layer?.summary?healthPills(shownPublished,true,layer.summary,absent):noDraft&&absent.length?absencePill(absent,live,layer?.people):null;
  target.append(footerStatus(rotaHealth||publishedHealth,attention>0?el('span',{class:'health-pill warn'},icon('alert'),el('span',{class:'num'},String(attention)),el('span',{class:'label'},` day${attention>1?'s':''} need${attention>1?'':'s'} attention`)):'',
    el('span',{class:'muted footer-total'},schedView==='year'||failed&&!draftData?'':(permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)?plural(live.length,'session'):plural(live.length,'duty','duties'))+' in view'),
    dayChanges?el('span',{class:'muted footer-total'},plural(dayChanges,'change')+' from published'):''));
}
// ---- Schedule helpers ----
/** The Schedule's filter fields, in the Filter popover's order, with their choices (from the catalogues). */
function scheduleFilterFields():{key:string;label:string;any:string;choices:Choice[]}[]{
  const tags=[...new Set(rows('people').flatMap(p=>(p.tags||[]) as string[]))].sort().map(t=>({value:t,label:t}));
  return [{key:'person_ids',label:'Person',any:'Anyone',choices:peopleChoices(ctx())},{key:'category_ids',label:'Rank',any:'Any rank',choices:choices(rows('categories'))},
    {key:'programme_ids',label:'Programme',any:'Any programme',choices:choices(rows('programmes'))},{key:'tags',label:'Tag',any:'Any tag',choices:tags},
    {key:'service_ids',label:'Service',any:'Any service',choices:choices(rows('services'))},{key:'location_ids',label:'Location',any:'Anywhere',choices:choices(rows('locations'))},
    {key:'assignment_type_ids',label:'Duty type',any:'Any duty',choices:choices(rows('assignment_types'))},
    {key:'consultation_mode',label:'Consultation mode',any:'Any',choices:[{value:'physical',label:'In person'},{value:'virtual',label:'Virtual'},{value:'unspecified',label:'Not recorded'}]}];
}
/** A saved view's filters; an older view's single place, duty type and person read as filters of one value. */
function filtersOf(p:Partial<PlannerSettings>|undefined):Record<string,string[]>{
  const out:Record<string,string[]>={...(p?.filters||{})};
  for(const [k,v] of [['location_ids',p?.location_id],['assignment_type_ids',p?.assignment_type_id],['person_ids',p?.person_id]] as [string,string|null|undefined][])if(v&&!out[k]?.length)out[k]=[v];
  // Fields in one order and values sorted, so the same filters compare equal (a view is not marked changed by order).
  return Object.fromEntries(Object.keys(out).sort().filter(k=>out[k].length&&!['only_open','only_issues'].includes(k)).map(k=>[k,[...out[k]].sort()]));
}
/** The Schedule's filters on its sessions: each field keeps any of its values (a place includes the places inside it;
 *  person facts (rank, programme, tag) are someone on the session's, on that day). */
function filterSessions(list:PlannerSession[]){
  if(exportFound&&!exportFound.values.length)return [];  // a People search that finds nobody lists no session
  const own=joinFilter(schedFilters,schedConditions),tree=exportFound?{match:'all' as const,conditions:[...(own?[own]:[]),exportFound]}:own;if(!tree)return list;
  // A place holds its own id and every place it is inside ("Clinic A" keeps the rooms in it).
  const parents=new Map(rows('locations').map(l=>[l.id,l.parent_id as string|undefined]));
  const within=(id:string)=>{const out:string[]=[];for(let at:string|undefined=id;at&&!out.includes(at);at=parents.get(at))out.push(at);return out;};
  const modes=new Map(rows('services').map(x=>[x.id,String(x.mode||'unspecified')]));
  const tags=new Map(rows('people').map(p=>[p.id,(p.tags||[]) as string[]]));
  const facts=(kind:string,key:string)=>{const by=new Map<string,Row[]>();for(const m of rows(kind)){const own=by.get(m.person_id)||[];own.push(m);by.set(m.person_id,own);}
    return (person:string,day:string)=>(by.get(person)||[]).filter(m=>activeOn(m,day)).map(m=>m[key]);};
  const ranks=facts('memberships','category_id'),programmes=facts('programme_memberships','programme_id');
  return list.filter(s=>{const day=toLocal(s.start,zone()).slice(0,10),ids=s.participants.map(p=>p.person_id);
    // The same facts the server reads (schedule_view._schedule_holds); person facts are the person's on that day.
    const person=(field:string,id:string):unknown[]=>field==='person_ids'?[id]:field==='category_ids'?ranks(id,day):field==='programme_ids'?programmes(id,day):field==='tags'?tags.get(id)||[]:[];
    const own=(field:string):unknown[]=>field==='service_ids'?s.service_ids:field==='location_ids'?within(s.location_id):field==='assignment_type_ids'?[s.assignment_type_id]
      :field==='consultation_mode'?(s.service_ids as string[]).map(id=>modes.get(id)||'unspecified'):field==='weekday'?[String((new Date(day+'T12:00:00Z').getUTCDay()+6)%7)]:[];
    const session=(field:string)=>[...own(field),...ids.flatMap(id=>person(field,id))];
    return holdsForSomeone(tree,session,ids.map(id=>(field:string)=>[...own(field),...person(field,id)]));});
}
/** The search, for the renderers that do not filter by themselves (Timeline, Day). */
function applyFilters(list:PlannerSession[]){
  return filterShifts(list,catalogues,{schema_version:1,mode:'week',search:schedSearch?'':searchText,location_id:null,assignment_type_id:null,person_id:null});
}
/** The session search with options (search.ts): over the sessions loaded for the days in view only, never a claim about
 *  other days (Schedule days have no server text search). Own text is the session's own (a closure reason); related
 *  are its duty type, place, clinics and people, each name on its own, or a picked one by id. */
function searchSessions(list:PlannerSession[]){
  const m=schedView==='people'||!schedSearch?null:recordMatcher(searchText,schedSearch);if(!m)return list;
  const named=(kind:string)=>new Map(rows(kind).map(r=>[r.id,labelOf(r)])),places=named('locations'),clinics=named('services'),people=named('people');
  return list.filter(s=>m({own:()=>[s.cancellation_reason],id:s.id,
    related:()=>[s.type_snapshot?.name,places.get(s.location_id),...(s.service_ids||[]).map((id:string)=>clinics.get(id)),...(s.participants||[]).map(x=>people.get(x.person_id))],
    refs:()=>[s.assignment_type_id,s.location_id,...(s.service_ids||[]),...(s.participants||[]).map(x=>x.person_id)]}));
}
const merged=(list:Row[],extra?:Record<string,Row>)=>{if(!extra)return list;const have=new Set(list.map(r=>r.id));return [...list,...Object.values(extra).filter(r=>!have.has(r.id))];};
/** Academic calendar entries in view: a labelled band above the People view (never behind an expander). */
// The academic calendar of the days in view, scoped by the server (staff see their own programmes; planners see every
// entry and whether confirmed members cover it). Read once per account, period and department input revision (a saved
// calendar entry or membership bumps it, so a refresh reads the new entries).
const academicCache=new Map<string,Promise<Row[]>>();
let academicRevision='';
async function academicBand(days:string[]):Promise<HTMLElement[]>{
  const revision=String(me?.organisation.input_revision??'');if(revision!==academicRevision){academicCache.clear();academicRevision=revision;}
  const start=days[0],end=addDays(days.at(-1)!,1),key=`${me?.identity_id}|${start}|${end}`;
  if(!academicCache.has(key))academicCache.set(key,get<{programme_calendar?:Row[]}>(`/api/calendar?start=${start}&end=${end}`).then(r=>r.programme_calendar||[],()=>{academicCache.delete(key);return [];}));
  const list=await academicCache.get(key)!;
  if(!list.length)return [];
  return [el('section',{class:'cal-academic','aria-label':'Academic calendar'},el('h3',{},`Academic calendar · ${plural(list.length,'entry','entries')}`),
    el('ul',{},...list.map(e=>el('li',{},`${e.programme_name}: ${e.name} · ${formatDate(e.starts_on,zone())} – ${formatDate(lastIncludedDate(e.ends_on),zone())}`,
      e.confirmed_members===0?badge('No confirmed members cover this period','warn'):''))))];
}
// Published days in view (planners): the stored check first, then the live coverage against today's availability.
let publishedLive:{key:string;evaluation:Evaluation}|null=null;
const liveKey=(layer:ScheduleWindow|null)=>layer?[...layer.sources.map(s=>s.id),layer.starts_on,layer.ends_on,me?.organisation.availability_revision,me?.organisation.input_revision].join('|'):'';
// One live check per publication (and account, rules and availability), whichever days are in view: moving between
// Week and Month filters it again instead of asking again. Only the current check of the publications in view is held:
// each is a whole publication's evaluation (megabytes for a published year) and every change elsewhere outdates it.
const liveChecks=new Map<string,{key:string;read:Promise<{evaluation:Evaluation}>}>();
function liveOf(p:Row){
  const key=[me?.identity_id,me?.organisation.availability_revision,me?.organisation.input_revision].join('|');
  let held=liveChecks.get(p.id);
  if(held?.key!==key){const read=get<{evaluation:Evaluation}>('/api/publications/'+p.id+'/coverage'),mine={key,read};held=mine;liveChecks.set(p.id,mine);
    read.catch(()=>{if(liveChecks.get(p.id)===mine)liveChecks.delete(p.id);});}
  return held.read;
}
function loadLiveCoverage(layer:ScheduleWindow){
  const key=liveKey(layer);if(publishedLive?.key===key||!layer.sources.length)return;
  for(const id of [...liveChecks.keys()])if(!layer.sources.some(s=>s.id===id))liveChecks.delete(id);
  const ids=new Set(layer.shifts.map(s=>s.id));
  void Promise.all(layer.sources.map(liveOf)).then(results=>{
    const findings=results.flatMap(r=>r.evaluation.findings.filter(f=>!f.shift_id||ids.has(f.shift_id))),coverage=results.flatMap(r=>r.evaluation.coverage.filter((c:Row)=>ids.has(c.shift_id)));
    publishedLive={key,evaluation:{valid:!findings.some(f=>f.severity==='error'),error_count:findings.filter(f=>f.severity==='error').length,warning_count:findings.filter(f=>f.severity!=='error').length,findings,coverage}};
    if(view==='schedule'&&!workspaceId)void redraw();
  }).catch(()=>{/* the stored check stays on screen */});
}
/** Print or export › CSV of a People view: the people its search finds, as one Person line under the Schedule's own
 *  filters (export only, never in the toolbar). */
let exportFound:Condition|null=null,schedulePeople:()=>Row[]=()=>rows('people');
/** Print or export: the view on screen, or another saved view or range drawn behind the dialog (one draw per week or
 *  month when pages break so) and copied; closing the dialog puts the Schedule back as it was. */
function exportView(){
  if(!scheduleExport)return;
  const before={definition:plannerDefinition(),id:plannerViewId,day:planDay,days:viewDays.days,draft:{workspaceId,draftNone,compareId}},ws=me!.organisation.week_start;
  // `open` ends when the dialog closes: a draw still running then stops changing the Schedule it put back.
  let moved=false,open=true;
  const back=()=>{exportFound=null;applyPlannerView(before.definition.planner,before.definition.conditions);plannerViewId=before.id;planDay=before.day;viewDays.days=before.days;({workspaceId,draftNone,compareId}=before.draft);};
  // The List's own columns (shown, order, wrap, widths) as they are: Adjust › Columns changes them for this export alone.
  const list=holdTableState('planner-list');
  const copy=():ExportSource=>({...scheduleExport!(),view:document.getElementById('schedule-view')!.cloneNode(true) as HTMLElement});
  // Drawn for paper: every person in a month's cells, every row of the List.
  const paper=async()=>{weekView.forPrint=true;try{await redraw();return copy();}finally{weekView.forPrint=false;}};
  // Changes made inside the dialog (View settings in display mode, People) for this export alone: laid over the chosen
  // view on every draw, dropped when the dialog closes. Saving them is the Schedule's own Save as view.
  // `axes`: Rows, Columns and Cells show (and the layout they make), applied through the saved-view reader so a repeated
  // dimension is dropped as everywhere (distinctAxes).
  type Axes=Pick<PlannerSettings,'mode'|'rows'|'columns'|'cells'>;
  let over:{week?:Partial<typeof weekView>;names?:PersonLabel|null;people?:typeof personDisplay;axes?:Axes}={},drawnView:string|null=null;
  const lay=()=>{if(over.axes)applyPlannerView(distinctAxes(plannerPart({...plannerDefinition().planner,...over.axes})),schedConditions);
    if(over.week)Object.assign(weekView,over.week);if('names' in over)viewNames=over.names??null;if(over.people)personDisplay=over.people;};
  const axesNow=():Axes=>{const p=plannerDefinition().planner;return {mode:p.mode,rows:p.rows,columns:p.columns,cells:p.cells};};
  // Back to the chosen view's own settings (Reset, or another view chosen in What); People stays: it is about the picked person.
  const forget=()=>{over={...(over.people?{people:over.people}:{})};list();};
  const looks=()=>({props:weekView.props,colourBy:weekView.colourBy,colourTags:[...weekView.colourTags],compact:weekView.compact,teamRows:weekView.teamRows,staffing:weekView.staffing});
  exportDialog({views:(plannerMemo?.views||[]).map(v=>({id:v.id,name:v.name})),rota:!!exportRota,canShare:permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN),
    publishedSheet:printSheet,
    adjust:scheduleSettings?(panel,close,changed)=>{
      // A choice shows at once in the popover (the state is the drawn one) and is kept in `over` for the next draws;
      // every choice (List columns too) ends in the popover's sync, which draws the preview again.
      const base=scheduleSettings!(close),keep=(week:Partial<typeof weekView>)=>{Object.assign(weekView,week);over.week=looks();};
      const axis=(set:()=>void)=>{set();over.axes=axesNow();};
      viewSettings({...base,display:true,saved:()=>'none',sync:changed,reset:()=>{forget();close();},
        rows:base.rows&&{...base.rows,set:r=>axis(()=>base.rows!.set(r)),setCells:c=>axis(()=>base.rows!.setCells(c)),setColumns:c=>axis(()=>base.rows!.setColumns(c))},
        colour:base.colour&&{...base.colour,set:k=>keep({colourBy:k as typeof weekView.colourBy}),tags:base.colour.tags&&{...base.colour.tags,set:ids=>keep({colourTags:ids})}},
        density:base.density&&{...base.density,set:k=>keep({compact:k==='compact'})},
        teamRows:base.teamRows&&{...base.teamRows,set:n=>keep({teamRows:n})},
        staffing:base.staffing&&{...base.staffing,set:on=>keep({staffing:on})},
        properties:{...base.properties,set:p=>keep({props:p})},
        label:{...base.label,set:l=>{viewNames=l;over.names=l;}}},panel,close);
    }:undefined,
    // A picked person (Search or Filter): who each duty lists in this export, the same three choices as Show.
    people:personPicked(schedSearch)?{options:Object.entries(PLANNER_PERSON_DISPLAYS) as [string,string][],value:()=>personDisplay,set:k=>{over.people=k as typeof personDisplay;}}:undefined,
    restore:()=>{open=false;weekView.forPrint=false;back();list();if(moved){moved=false;void redraw();}},
    draw:async o=>{
      if(!open)return [];
      back();
      moved=true;  // always drawn again, for paper
      const saved=o.view_id&&plannerMemo?.views.find(v=>v.id===o.view_id);
      if(saved){applyPlannerView(saved.definition.planner,saved.definition.conditions);plannerViewId=saved.id;}
      // Another view chosen in What starts from its own settings (People stays: it is about the picked person).
      if(o.view_id!==drawnView){drawnView=o.view_id;forget();}
      lay();
      // CSV is data: a view without a table (Calendar, Day, People, Timeline, Year) is drawn as the List of the same days and filters.
      if(o.table&&schedView!=='list'&&schedView!=='coverage'){
        // People searches people, not sessions (its session search is not in use there): the List keeps the sessions of the
        // people that search finds, the People view's own rows.
        if(schedView==='people'){const q=peopleView.find.trim().toLowerCase();searchText='';schedSearch=undefined;
          if(q)exportFound={field:'person_ids',op:'any',values:schedulePeople().filter(p=>findsPerson(p,q,shortCode)).map(p=>p.id as string)};}
        setLayoutRange('list',rangeNow());}
      const shown=schedView,layout=shown==='year'?'calendar':rendererOf(shown),rota=exportRota;
      if(o.range==='rota'&&rota){planDay=String(rota.starts_on);setLayoutRange(layout,'custom');viewDays.days=Math.round((Date.parse(rota.ends_on)-Date.parse(weekOf(planDay,viewDays.week_start??ws)))/864e5);}
      else if(o.range!=='view'&&o.range!=='rota')setLayoutRange(layout,o.range);
      // Pages per week or month keep the view's layout, one draw per chunk; a month already pages by week (its week bands); a year is one sheet.
      const chunk=shown==='year'||shown==='month'&&o.chunk==='week'?null:o.chunk;
      if(!chunk)return [await paper()];
      const days=periodNow(planDay,ws).days,starts=[...new Set(days.map(d=>chunk==='week'?weekOf(d,viewDays.week_start??ws):d.slice(0,8)+'01'))],out:ExportSource[]=[];
      for(const start of starts){if(!open)return [];planDay=start;setLayoutRange(layout,chunk);out.push(await paper());}
      return out;
    }});
}
/** Published rota tools (the old Published rota page): print, change, earlier versions. */
function publishedTools(item:(label:string,glyph:IconName,run:()=>unknown)=>HTMLElement,change=true):HTMLElement[]{
  const out:HTMLElement[]=[];
  // One entry for paper (SR-11): one dialog for this view or another, PDF, PNG or CSV, and the published sheet.
  out.push(item('Print or export…','printer',exportView));
  if(change&&permits(CAPABILITY.COORDINATOR))out.push(item('Change a published rota…','edit',()=>void changePublished()));
  if(may('publications.read_team'))out.push(item('Published versions…','history',()=>void publishedVersions()));
  return out;
}
async function publishedInView(){const list:Row[]=(await get<{publications:Row[]}>('/api/publications')).publications;return list.filter(p=>p.current);}
function printSheet(){
  void publishedInView().then(current=>showDialog('Print sheet','',(body,close)=>{
    body.classList.add('form-grid');
    if(!current.length){body.append(el('div',{class:'span-2'},empty('Nothing published yet','Once a rota is approved and published, its print sheet is made here.',null,'printer')));return;}
    const around=current.find(p=>p.starts_on<=planDay&&planDay<(p.effective_until||p.ends_on))||current[0];
    const pub=select('Published rota',current.map(p=>({value:p.id,label:`${p.name} · ${formatDate(p.starts_on,zone())} – ${formatDate(lastIncludedDate(p.ends_on),zone())}`})),around.id);
    pub.wrap.classList.add('span-2');
    const layout=select('Print period',[{value:'day',label:'Day'},{value:'week',label:'Week'},{value:'month',label:'Month'}],'week');
    const on=field('Print date','date',planDay,{required:true});
    const full=may('publications.read_team'),filters=el('div',{class:'form-grid','aria-live':'polite'});
    let loadedId='',service:ReturnType<typeof multi>|null=null,person:Field<HTMLSelectElement>|null=null;
    const open=button('Open print sheet',()=>{
      if(loadedId!==pub.input.value||!on.input.reportValidity())return;
      const params=new URLSearchParams({format:'print',layout:layout.input.value,on:on.input.value});
      for(const id of service?.values()||[])params.append('service_ids',id);if(person?.input.value)params.set('person_id',person.input.value);
      window.open('/api/reports/'+loadedId+'?'+params,'_blank','noopener');
    },'primary',{disabled:true,why:'Choose a publication first.'});
    const read=newest((id:string,current:unknown)=>publicationCache(id,current).data);
    const load=async()=>{
      const selected=current.find(p=>p.id===pub.input.value)!;
      loadedId='';service=null;person=null;open.disabled=true;filters.replaceChildren(skeleton('lines','Loading print options…'));
      try{
        const data=await read(selected.id,selected.current);if(!data||!body.isConnected||pub.input.value!==selected.id)return;
        // Names and allowed filters belong to this published version, including people and clinics since renamed or retired.
        const people=choices(Object.values(data.snapshot.people),true);
        const clinics=choices(Object.values(data.snapshot.services)).sort(byLabel);
        // S-23: the label is "Clinics"; what no tick means is said once beside it, not in the label.
        if(full&&clinics.length){service=multi('Clinics',clinics);service.wrap.append(el('small',{class:'hint'},'None ticked prints every clinic.'));}
        if(full&&people.length){person=select('Print person',people,'',false,'All people');person.wrap.classList.add('span-2');}
        filters.replaceChildren();if(service)filters.append(service.wrap);if(person)filters.append(person.wrap);
        loadedId=selected.id;open.disabled=false;
      }catch(error){if(body.isConnected&&pub.input.value===selected.id)filters.replaceChildren(
        readFailed('the print options',error,load));}
    };
    // Cancel and the primary in a footer, as every dialog (S-23).
    body.append(pub.wrap,layout.wrap,on.wrap,filters,el('div',{class:'form-footer span-2'},button('Cancel',close,'secondary'),open));
    pub.input.addEventListener('change',()=>void load());void load();
  })).catch(reportError);
}
/** Change the published rota in force on the day in view: a draft amendment of it (the overlap choice's third option). */
async function changePublished(){
  const current=await publishedInView(),p=current.find(x=>x.starts_on<=planDay&&planDay<(x.effective_until||x.ends_on));
  if(!p){notify('No published rota is in force on this day. Move to a published day first.','warning');return;}
  workspaceForm(ctx(),{...p,name:p.name});
}
async function publishedVersions(){
  const list:Row[]=(await get<{publications:Row[]}>('/api/publications')).publications;
  dockPanel({key:'published-versions',title:'Published versions',sub:'Each change to a published rota is a new version; earlier ones stay readable.',build:({body,close})=>{
    // A list of versions in the docked panel (DESIGN: lists are panels, not modals); each opens its version.
    body.append(list.length?el('ul',{class:'dock-list','aria-label':'Published versions'},...list.map(p=>{
      const b=el('button',{type:'button',class:'menu-item'},el('span',{class:'issue-text'},el('strong',{},`${p.name} · version ${p.sequence}`),el('small',{},`${formatDate(p.starts_on,zone())} – ${formatDate(lastIncludedDate(p.effective_until||p.ends_on),zone())} · ${p.current?'current':'superseded'}`)),badge(p.current?'Current':'Superseded',p.current?'ok':''));
      b.addEventListener('click',()=>{close();planDay=String(p.starts_on);switchView('month');sourcePublication=p.current?'':p.id;workspaceId='';draftNone=true;void renderPage();});
      // A version that replaced another says what it changed, day by day (SR-35), without the audit log.
      const what=p.supersedes_id?el('button',{type:'button',class:'menu-item menu-sub'},icon('history'),el('span',{},`What changed in version ${p.sequence}`)):null;
      what?.addEventListener('click',()=>{void publicationChanges(p).catch(reportError);});  // opens within: Back to the list
      return el('li',{},b,what);})):empty('Nothing published yet','Published rotas and their versions appear here.',null,'history'));
  }});
}
async function publicationChanges(p:Row){
  const days:Row[]=(await get<{days:Row[]}>(`/api/publications/${encodeURIComponent(p.id)}/changes`)).days;
  dockPanel({key:'publication-changes',title:`What changed · ${p.name} version ${p.sequence}`,build:({body})=>{
    body.append(days.length?table(['Day','Added','Removed','Changed'],days.map(d=>[formatDate(d.date,'UTC'),String(d.added),String(d.removed),String(d.changed)]))
      :empty('No session changed','This version has the same sessions and people as the one it replaced.',null,'history'));
  }});
}
/** The overlap panel (footer pill): where this draft fits beside the published rotas, and the drafts competing with it. */
async function overlapDialog(data:WorkspaceData){
  const w=data.workspace,o=await get<Overlaps>('/api/schedule/overlaps?workspace_id='+w.id);
  const editable=w.status==='draft'&&permits(CAPABILITY.COORDINATOR);
  const range=(from:string,to:string)=>[formatDate(from,zone()),'– '+formatDate(lastIncludedDate(to),zone())];
  const shown=formDialog(o.conflict?'These days already have a published rota':'Other drafts cover these days',`${w.name} · ${range(w.starts_on,w.ends_on).join(' ')}`,form=>{
    const choice=o.conflict&&editable?overlapChoice(o,zone(),()=>{relabel(submit,choice!.verb('Use these dates'));}):null;
    if(choice)form.append(choice.root);else if(o.conflict)form.append(note('A coordinator chooses how this draft fits before it is published.','info'));
    for(const d of o.drafts){const b=button('Compare',()=>{(form.closest('dialog') as HTMLDialogElement|null)?.remove();compareId=d.id;layerMode='side';void redraw();},'secondary small');
      const row=el('div',{class:'rc-issue span-2'},icon('layers'),el('span',{class:'issue-text'},el('strong',{},d.name),el('small',{},`${d.status==='draft'?'Draft':d.status==='submitted'?statusLabel('submitted'):'Approved'} · `,dateUnits(range(d.overlap.starts_on,d.overlap.ends_on)))),b);
      // A duplicate draft goes from here, as from Rota tools (its message carries Undo).
      if(editable&&d.status==='draft')row.append(button('Discard draft',async()=>{const r=(await rotaMeta(d.id)).workspace;
        await command('workspace.archive',{reason:`Not used: ${w.name} covers these days`},d.id,r.revision);row.remove();notify(`${d.name} discarded.`);void refresh();},'ghost small',{'aria-label':`Discard ${d.name}`}));
      form.append(row);}
    const submit=form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]')||el('button');
    // Nothing to choose: the dialog only informs, so its one way out reads Close.
    queueMicrotask(()=>{const d=form.closest('dialog'),s=d?.querySelector<HTMLButtonElement>('button[type=submit]');if(s){if(!choice)s.hidden=true;else relabel(s,choice.verb('Use these dates'));}
      const cancel=[...(d?.querySelectorAll<HTMLButtonElement>('button')||[])].find(b=>b.textContent?.trim()==='Cancel');if(cancel&&!choice)relabel(cancel,'Close');});
    return async()=>{if(!choice)return;await applyOverlap(w,choice.value());};
  },'Use these dates');
  // A date's day, month and year stay together on a phone; the range may wrap between dates (AO-006).
  shown.dialog.querySelector('.dialog-head p')?.replaceChildren(`${w.name} · `,dateUnits(range(w.starts_on,w.ends_on)));
}
/** Applies an overlap choice to a saved draft: new dates (Undo puts them back), or an amendment draft of the published rota. */
async function applyOverlap(w:Row,v:OverlapValue|null){
  if(!v)throw new Error('Choose how this draft fits.');
  if(v.choice==='amend'){const p=(await publishedInView()).find(x=>x.id===v.publication_id);if(p)workspaceForm(ctx(),{...p});return;}
  await command('workspace.resolve_overlap',{choice:v.choice,...(v.choice==='replace_from'?{starts_on:v.starts_on}:{}),reason:v.choice==='start_after'?'Start after the published rota':'Replace the published rota from '+v.starts_on},w.id,w.revision);
  notify(v.choice==='start_after'?`The draft now starts on ${formatDate(v.starts_on,zone())}.`:`The draft replaces the published rota from ${formatDate(v.starts_on,zone())}.`);
  planDay=v.starts_on;await refresh();
}
/** Publish…: checks the draft first (rules, open places, overlaps that changed since it was started, competing drafts),
 *  then takes the next governed step: submit a draft for review, or publish an approved rota. */
/** What Submit, Publish and Approve read before they open: the rota's overlaps and its Fix list (the server's one object). */
async function readRotaReview(data:WorkspaceData){
  const [o,fix]=await Promise.all([get<Overlaps>('/api/schedule/overlaps?workspace_id='+data.workspace.id),readFixList(data.workspace.id),readJobs(data)]);
  return {o,fix};
}
/** What a submitted rota carries for whoever decides it (one component: the review strip, Approve and Publish): the
 *  coordinator's note (ME-25, `note` false while submitting) and, for an amendment, what it changes session by session,
 *  was → now with the reason (F4 / ME-26). */
function submittedFacts(data:WorkspaceData,note=true):HTMLElement[]{
  const sent=note?(data.review_progress as {submission?:{note?:string|null}|null}|undefined)?.submission?.note:null;
  const diff=changedSessions((data as {change_diff?:ChangeDiff|null}).change_diff,{zone:zone(),name:id=>name('people',id),place:x=>name('locations',x.location_id)});
  return [...(sent?[el('section',{class:'span-2','aria-label':'Submitted with this note'},el('h3',{},'Submitted with this note'),el('blockquote',{class:'fix-quote'},`“${sent}”`))]:[]),...(diff?[diff]:[])];
}
/** The rota as the person deciding sees it (A5/A6; one component for Submit, Publish and Approve): a few tiles, the Fix
 *  list's gate and its must-fix causes, read only (the work happens on the Fix list). Submit may carry gaps that are not
 *  safety rules, each with a reason (D3); the approver reads those reasons and decides them, with a reason of their own.
 *  Returns what the command adds and whether it may go ahead; `changed` runs when that changes. */
function rotaReviewSummary(form:HTMLElement,data:WorkspaceData,{o,fix}:Awaited<ReturnType<typeof readRotaReview>>,mode:'submit'|'publish'|'approve',opt:{gaps?:boolean;changed?:()=>void}={}){
  const w=data.workspace,close=()=>(form.closest('dialog') as HTMLDialogElement|null)?.remove(),s=fix.summary;
  const must=fix.causes.filter(c=>c.level==='must'),hooks={...fixHooks(w.id,data),act:undefined};
  const sessions=data.shifts.filter(x=>!x.cancelled&&!x.context).length,prerequisite=mode==='submit'?s.submission_issue:null;
  const item=(k:string,v:string,tone='')=>el('div',{class:'summary-item '+tone},el('span',{},k),el('strong',{},v));
  const replaced=o.publications.find(p=>p.relation==='replaces');
  form.append(el('div',{class:'span-2 publish-summary'},item('Sessions',String(sessions)),
    item('Places filled',s.places_total?`${s.places_filled} of ${s.places_total}`:'None to fill',s.places_filled<s.places_total?'warn':''),
    // SW-21 (A5): the Fix list's could-improve count and, once nothing must be fixed (A7), the score, from the same summary.
    s.could?item('Could improve',String(s.could)):'',s.score!=null&&!s.must?item('Score',String(s.score)):''));  // SW3-A-25: the server's one-decimal score, as the footer
  form.append(...submittedFacts(data,mode!=='submit'));
  // Every rota adjustment (D16) is reviewed once with the rota, its justification quoted; the approver decides each.
  const items=reviewItemsOf(fix,data,data.workspace),reviewHooks={...hooks,personName:(id:string)=>name('people',id)};
  // maintenance/maintenance (P2-BE): with scheduling groups, the review reads in one section per group, each with its fill and state.
  const planning=planningOf(fix,data),groups=planning?.groups;
  const waiting=mode==='submit'?proposalNote(data,close):null;if(waiting)form.append(waiting);
  if(prerequisite)form.append(el('div',{class:'span-2 issues-cta'},note(prerequisite.message,'warning'),button('Add session',()=>{close();shiftForm(ctx(),data);},'secondary small')));
  if(replaced)form.append(note(`Replaces ${replaced.name} from ${formatDate(w.starts_on,zone())}. ${replaced.name} stays in force until then.`,'info'));
  const state={ack:false,blocked:()=>!!prerequisite||mode!=='publish'&&mode!=='approve'&&must.length>0&&!state.ack,
    payload:():Record<string,unknown>=>({}),gaps:0,label:()=>''};
  // Approving: every rota adjustment and every gap the coordinator acknowledged, each with its justification; the
  // approver approves them with the rota, or rejects single items, which returns the rota to its coordinator (maintenance).
  if(mode==='approve'){
    // R5: the approver decides each item (accept, or reject with a reason); the primary follows the decisions and waits
    // until all are decided: Approve rota, or Return to coordinator when any is rejected.
    const dialog=()=>form.closest('dialog'),count=el('span',{class:'decide-count',role:'status',id:`decide-count-${w.id}`});
    const sync=()=>{const b=dialog()?.querySelector<HTMLButtonElement>('button[type=submit]'),rejected=review.rejected();
      const undecided=!!review.total&&!review.decisions();
      // L11: a disabled primary says why (its description): every item needs a decision, a reject its reason.
      count.replaceChildren(review.total?factLine([!undecided?'':review.decided()<review.total?'Decide each item to approve':'A reject needs its reason',`${review.decided()} of ${review.total} decided`,rejected?`${rejected} rejected`:'']):'');
      if(b){b.textContent=state.label();b.disabled=undecided;if(undecided)b.setAttribute('aria-describedby',count.id);else b.removeAttribute('aria-describedby');}
      if(!count.isConnected)dialog()?.querySelector('.form-footer')?.prepend(count);};
    const review=reviewedWithRota(items,fix.causes,reviewHooks,{decide:true,changed:sync,groups});
    // maintenance: the rules that bind groups together hold (or not), said once before the sections.
    const linked=(data as {linked_summary?:{count:number}|null}).linked_summary||planning?.held,coupled=planning?.counts?.coupling||0;
    if(linked?.count)form.append(el('p',{class:'g-held span-2',role:'note'},icon(coupled?'alert':'check'),coupled?`${plural(coupled,'linked cause')} · ${plural(linked.count,'session')} shared by groups`:`Linked rules hold · ${plural(linked.count,'session')} shared by groups`));
    // Freshness from the Fix list read now: nothing new since it was submitted, or the causes that are (never approved
    // as gaps here: the out-of-date pill asks to resubmit).
    const open=must.filter(c=>!c.acknowledgement);
    form.append(el('div',{class:'span-2'},open.length?fixGate(s,el('span',{},el('strong',{},`${mustWords(open.length)}`),' new since it was submitted'))
      :fixGate({...s,must:0,places_total:0},el('span',{},el('strong',{},'Nothing new since it was submitted'),s.could?` · ${couldWords(s.could)}`:''))));
    if(open.length)form.append(el('div',{class:'span-2 publish-issues'},fixLevel('must',open,hooks,{tag:'h3',panel:false})));
    form.append(...review.nodes);
    state.gaps=review.total;
    state.label=()=>review.rejected()?TERMS.returnToCoordinator:data.workspace.base_publication_id?'Approve changes':'Approve rota';
    // maintenance: every current review item decided exactly once (accepted items carry the overall review reason).
    state.payload=()=>{const d=review.decisions();if(review.total&&!d)throw new Error('Decide every item: accept it, or reject it with a reason.');
      return d?.length?{decisions:d}:{};};
    queueMicrotask(sync);
    return state;
  }
  // Submit and Publish list the adjustments the approver will see, read only.
  const shown=reviewedWithRota(items.filter(i=>i.kind!=='acknowledged_gap'),fix.causes,reviewHooks,{groups});
  if(!must.length){form.append(...shown.nodes);return state;}
  // Submitting: the gate says what stops it, with the way there. A cause that is not a safety rule may be submitted
  // as an acknowledged gap with its own reason (D3); when every remaining cause allows it, one tick offers that.
  const open=button('Open Fix list',()=>{close();openIssues(w.id);},'secondary small');
  // D23: which groups still have must-fix causes (any group owner submits once none has).
  const blocking=(groups||[]).filter(g=>g.must);
  const lead=blocking.length?el('span',{},el('strong',{},new Intl.ListFormat('en').format(blocking.map(g=>g.name))),` must fix ${plural(must.length,'cause')} first`)
    :el('span',{},el('strong',{},plural(must.length,'cause')),' must be fixed first');
  const gate=fixGate({...s,places_total:0,checked_at:null},lead);gate.append(open);
  const ackable=mode==='submit'&&must.every(c=>c.acknowledgeable),some=mode==='submit'&&must.some(c=>c.acknowledgeable);
  const reasons=new Map<string,HTMLTextAreaElement>(),gateKey=legend('Key'),keyed={...hooks,key:gateKey};  // the rows' level marks
  const rows=must.map(c=>{
    if(!ackable)return fixRow(c,keyed,{extra:some&&!c.acknowledgeable&&!c.safety?el('p',{class:'gap-safety'},icon('shield'),'Can’t be acknowledged'):''});
    const id=`gap-${c.id}`,input=el('textarea',{id,rows:2,maxlength:500,placeholder:'Required'}) as HTMLTextAreaElement;reasons.set(c.id,input);
    const field=el('div',{class:'gap-reason'},el('label',{for:id},'Why submit with this gap'),input);field.hidden=true;
    return fixRow(c,keyed,{extra:field});});
  const box=ackable?check('Submit with these gaps acknowledged',!!opt.gaps):null;
  const sync=()=>{state.ack=!!box?.input.checked;for(const li of rows)li.querySelector<HTMLElement>('.gap-reason')?.toggleAttribute('hidden',!state.ack);
    for(const t of reasons.values())t.required=state.ack;opt.changed?.();};
  box?.input.addEventListener('change',sync);
  state.gaps=must.length;
  state.payload=()=>{if(!state.ack)return {};const list=[...reasons].map(([cause_id,t])=>({cause_id,reason:t.value.trim()}));
    const missing=list.find(x=>x.reason.length<3);if(missing){reasons.get(missing.cause_id)!.focus();throw new Error('Give a reason for each gap.');}
    return {acknowledgements:list};};
  form.append(el('div',{class:'span-2 publish-issues'},gate,box?el('div',{class:'gap-ack'},box.wrap):'',el('ul',{class:'fix-list','aria-label':'Must fix'},...rows),gateKey.root));
  form.append(...shown.nodes);
  queueMicrotask(sync);
  return state;
}
async function publishDialog(data:WorkspaceData,opt:{gaps?:boolean}={}){
  let w=data.workspace;
  if(w.archived_at)throw new Error('Restore the archived rota before submitting it for review.');
  // The dialog belongs to the page and rota it was asked from: leaving either cancels it (LIVE-01).
  // SW3-B-02: the page, not the whole address: a redraw that rewrites the week or a query parameter meanwhile is not
  // leaving, and returning then left Submit doing nothing at all.
  const page=()=>location.hash.split(/[/?]/)[0],here=page(),rota=w.id,left=()=>page()!==here||workspaceId!==rota;
  // A rota check changes nothing, so it runs at once when the stored one is outdated (the button reads "Checking…").
  if(w.validation?.fingerprint!==data.current_fingerprint&&w.status==='draft'){
    await command('workspace.validate',{reason:'Check before publishing'},w.id,w.revision);if(left())return;
    data=await rotaWhole(w.id);w=data.workspace;}
  // A read that failed says so, with Retry, instead of the button going quiet (SW3-B-02).
  const read=await readRotaReview(data).catch(e=>{if(left()||sessionChanged(e))return null;
    notify(couldntLoad('the rota’s checks',e),'error',{label:'Retry',run:()=>publishDialog(data,opt)},e);return null;});
  if(!read||left())return;
  const {o}=read,approved=w.status==='approved';
  formDialog(approved?`Publish ${w.name}`:`Submit ${w.name} for review`,approved?'Staff are told as soon as you publish.':'',form=>{
    // Submit is named for what it does: with acknowledged gaps it says how many, and is pressed twice.
    const label=()=>review.ack?`Submit with ${plural(review.gaps,'gap')}`:approved?'Publish':'Submit for review';
    const review=rotaReviewSummary(form,data,read,approved?'publish':'submit',{...opt,changed:()=>{const s=submit();s.disabled=review.blocked();relabel(s,label());if(review.ack&&!s.dataset.armedGaps){s.dataset.armedGaps='1';armed(s,'Confirm submit with gaps?');}}});
    // Things changed since you started: a published rota now overlaps (e.g. it was amended), or other drafts cover these days.
    const choice=o.conflict?overlapChoice(o,zone(),()=>relabel(submit(),choice!.verb(approved?'Publish':'Submit'))):null;
    const competing=o.drafts.map(d=>({d,discard:approved&&permits(CAPABILITY.COORDINATOR)&&d.status==='draft'?check(`Discard ${d.name} after publishing`,false):null}));
    if(choice||competing.length){const box=el('fieldset',{class:'form-section span-2'},el('legend',{},'Changed since you started'));
      if(choice)box.append(choice.root);
      for(const {d,discard} of competing){const b=button('Compare',()=>{(form.closest('dialog') as HTMLDialogElement|null)?.remove();compareId=d.id;layerMode='side';void redraw();},'secondary small');
        box.append(el('div',{class:'rc-issue'},icon('layers'),el('span',{class:'issue-text'},el('strong',{},`${o.drafts.length===1?'Another draft covers':`${o.drafts.length} drafts cover`} these days`),el('small',{},`${d.name} · ${d.status==='draft'?'Draft':d.status==='submitted'?statusLabel('submitted'):'Approved'}`)),b),...(discard?[discard.wrap]:[]));}
      form.append(box);}
    // ME-25: a note, not a decision; a submit note is shown to the approver.
    const reason=area(approved?'Note (optional)':'Note for the approver (optional)','',2);reason.input.maxLength=500;reason.wrap.classList.add('span-2');
    form.append(reason.wrap);
    const submit=()=>form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]')||el('button');
    // SW-12: the long save says what it does and, past 3 s, for how long (loading.ts busy).
    queueMicrotask(()=>{const s=submit();if(choice)relabel(s,choice.verb(approved?'Publish':'Submit'));s.dataset.busy=approved?'Publishing…':'Submitting…';s.disabled=review.blocked();if(approved)armed(s,'Confirm publish?');});
    return async()=>{
      if(choice){const v=choice.value();if(v?.choice==='amend'){await applyOverlap(w,v);return;}await applyOverlap(w,v);w=(await rotaMeta(w.id)).workspace;}
      const why=routineReason(reason.input.value);
      if(!approved){const gaps=review.payload();await command('workspace.submit',{reason:why,...gaps},w.id,w.revision);notify(review.ack?`Submitted for review with ${plural(review.gaps,'acknowledged gap')}`:'Submitted for review');await refresh();return;}
      const result=await command('workspace.publish',{reason:why},w.id,w.revision);
      let discarded=0;
      for(const {d,discard} of competing)if(discard?.input.checked)await command('workspace.archive',{reason:`Not used: ${w.name} was published`},d.id,(await rotaMeta(d.id)).workspace.revision).then(()=>{discarded++;},reportError);
      const stale=(result.outdated_drafts||[]) as Row[];
      // The Undo here (if any) is the discards': publishing itself is not undone. The dialog closes first, so the
      // confirmation is said on the published rota, not inside a closing dialog behind "Saving…" (ME-24).
      form.closest('dialog')?.remove();
      // SW3-B-07: said as soon as the server confirms, not after the reads that redraw the page (minutes under load).
      const said=refresh();
      notify('Published · staff are told.'+(discarded?` Undo restores the ${discarded===1?'discarded draft':`${discarded} discarded drafts`}.`:'')+(stale.length?` ${stale.map(x=>x.name).join(', ')} now overlap${stale.length===1?'s':''} a published rota; open ${stale.length===1?'it':'them'} to choose how ${stale.length===1?'it fits':'they fit'}.`:''));
      await said;
    };
  },approved?'Publish':'Submit for review',(error,close)=>{
    if(!approved&&error instanceof ApiError&&error.code==='VALIDATION_FAILED'
      &&(error.details as {refresh_baseline?:boolean}|undefined)?.refresh_baseline)
      return button('Refresh',()=>{close();return refreshDraftBaseline(w,refresh).catch(reportError);},'secondary small');
  });
}

/** Compact rota health: open places, blocking issues and warnings. Each opens a list you can act on. */
/** The planner week page that shows a session's day. */
/** A restored rota opens on the schedule, on its days (Archived rotas › Restore): the address is written before the panel closes. */
function restoredRota(w:Row){ctx().setWorkspace(w.id);
  if(!(planDay>=w.starts_on&&planDay<w.ends_on))planDay=String(w.starts_on);
  writeRoute();return refresh();}
/** Show a session on the calendar grid: keep the current range if already there, otherwise its week. */
function toCalendar(s:Row){
  if(!['week','day'].includes(schedView))switchView('week');
  planDay=toLocal(s.start,zone()).slice(0,10);
}
/** Rota health from the rota list's summary, for Year and Month (the draft's sessions are not loaded there). Each pill
 *  opens the List view filtered to what it counts. */
const EXTRA_COVER_OPEN=(n:number)=>`${plural(n,'extra-cover session')} open`;
/** OPEN_EXTRA_COVER findings: one per uncovered interval and requirement, so a session can have several. */
const isExtraCover=(f:Row)=>f.code==='OPEN_EXTRA_COVER'&&f.severity!=='error';
/** Rota health (A2/A7): the server's Fix list counts, never recounted here. Each pill opens the Fix list's head;
 *  Open fix list opens the page. The score is the server's: in the Ready pill once nothing must be fixed, else its own
 *  pill beside the must-fix count, capped and saying how many block it reviewed. */
/** SW3-B-11: the reader's own next step on a rota, which "Ready" leads to: a coordinator submits a draft, whoever
 *  decides the pending step approves it, an approved rota is published; anyone else gets no verb. */
const readyStep=(w:Row|null|undefined,data:WorkspaceData|null):FixStep=>{
  if(!w)return null;
  if(w.status==='draft')return w.purpose!=='sandbox'&&permits(CAPABILITY.COORDINATOR)?'submit':null;
  if(w.status==='submitted')return !!data&&permits(CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)&&decidesStep(data)?'approve':null;
  return w.status==='approved'&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)?'publish':null;};
function fixHealth(id:string,s:FixSummary,data:WorkspaceData|null,w:Row|null|undefined=data?.workspace){
  // maintenance: with scheduling groups the footer counts the group shown ("Consultants · ⚠ 3 must fix"), the server's per-group
  // numbers; the score is the whole rota's, so it waits for All groups.
  const g=scopedGroup(id,planningOf(data));
  // A7: once nothing must be fixed, the ready popover's Score breakdown opens the score's own "Why this score".
  const score=data?.workspace.validation?.score,byId=new Map((data?.shifts||[]).map(x=>[x.id,x]));
  const breakdown=score&&shownScore(score)!=null?()=>{const at=document.querySelector<HTMLElement>('.app-footer .fix-pills .health-pill.ok');if(!at)return;
    const review=w?.status==='draft'?reviewScores(id):undefined;
    popover(at,(panel,close)=>panel.append(...scoreWhy(score,pid=>name('people',pid),sid=>{close();const x=byId.get(sid);if(x&&!openSession(x))readOnlySession(x);},review&&(()=>{close();review();}))),{label:'Why this score',placement:'below-end'});}:undefined;
  // V08: a summary without a score (an applied run's command_changes check and its fix_list_after say score null) shows
  // the rota's current check's score, which the complete check patches into the held draft (applyCheckDelta).
  const pills=fixPills(g?{...s,must:g.must,could:g.could,score:null}:{...s,score:s.score??(score?shownScore(score):null)},{load:()=>readFixList(id),held:()=>{const h=fixCache.get(id)?.list;return h&&h.summary.must===s.must&&h.summary.could===s.could?h:heldFixList(id,s);},open:()=>openIssues(id),breakdown,hooks:fixHooks(id,data,w),step:readyStep(w,data)});
  if(g)pills.prepend(el('span',{class:'footer-group'},g.name));
  if(data&&proposalShown(data))pills.prepend(proposalPill(data));
  // Owner, 7 Nov 2026: the score shows whenever the rota has one, not only once nothing must be fixed. Capped, it says
  // only "capped": the must-fix pill beside it gives the one count (two different numbers side by side confused).
  if(!g&&s.must&&score&&shownScore(score)!=null){
    const pill=scorePill(id+':'+(data?.shifts.length??0),score,pid=>name('people',pid),sid=>{const x=byId.get(sid);if(x&&!openSession(x))readOnlySession(x);},w?.status==='draft'?reviewScores(id):undefined);
    const said=`Schedule score ${shownScore(score)} of 100, capped while anything must be fixed: ${mustWords(s.must)}`;
    pill.setAttribute('aria-label',said);pill.dataset.ctip=said;
    pill.append(el('span',{class:'pill-extra score-blocking','aria-hidden':'true'},' · capped'));pills.append(pill);}
  return pills;
}
/** F-16: an out-of-date check keeps its last counts (never blanked), dimmed and said to be from the last check,
 *  beside the pill that checks again. */
function staleHealth(pills:HTMLElement,check:HTMLElement){
  pills.classList.add('fix-stale');
  for(const b of pills.querySelectorAll<HTMLElement>('.health-pill'))b.setAttribute('aria-label',(b.getAttribute('aria-label')||'')+' · from the last check');
  pills.append(check);return pills;}
const pillCounts=new Map<string,string>();
/** The rota's issues as issue cards (footer health lists, publish checks): what is wrong, which session, who, which
 *  rule and what to do. `done` closes whatever list the card sits in before an action moves the view. */
function rotaIssues(data:WorkspaceData,done:()=>void=()=>{},sessions?:SessionSummary[]){
  const v=data.workspace.validation!,byId=new Map(data.shifts.map(s=>[s.id,s]));
  const when=(s:Row)=>formatDate(s.start,zone())+' · '+formatTime(s.start,zone())+'–'+formatTime(s.end,zone());
  const editable=data.workspace.status==='draft'&&permits(CAPABILITY.COORDINATOR);
  const show=(s:Row|undefined)=>s?button('Show on schedule',()=>{done();showOnSchedule(s);},'secondary small'):null;
  // Draft cards use the summary's per-session places; published cards keep their issued coverage evidence.
  const short:Partial<SessionSummary>[]=sessions?sessions.filter(x=>x.has_gap):[...new Set<string>((v.coverage||[]).filter((r:Row)=>r.status==='gap').map((r:Row)=>r.shift_id))].map(shift_id=>({shift_id}));
  const gaps:Issue[]=short.filter(x=>byId.has(x.shift_id!)).map(x=>{
    const s=byId.get(x.shift_id!)!,clinic=(s.service_ids as string[]).map(c=>name('services',c)).join(' + ');
    return {severity:'warning',shift:s.id,words:`${x.places_open?plural(x.places_open,'place')+' open':'Open places'} — ${[clinic,s.type_snapshot.name].filter(Boolean).join(', ')}`,
      session:`${when(s)} · ${name('locations',s.location_id)}${x.places_needed?` · ${x.places_filled} of ${plural(x.places_needed,'place')} filled`:''}`,
      actions:[show(s),editable&&!s.locked?button('Pick someone…',()=>{done();queueFill(data.workspace.id,s.id);toCalendar(s);void renderPage();},'secondary small'):null]};});
  // Each finding names its person and session (duty, day and time), so a long list of the same message stays tellable apart.
  const finding=(f:Row)=>{const s=f.shift_id?byId.get(f.shift_id):undefined;return findingCard(f,s,findingActions(data,f,s,done));};
  // An advisory issue accepted on Resolve issues is not a warning any more.
  const accepted=new Set((data.workspace.accepted_issues||[]).map((a:Row)=>a.key));
  // A session short of people is its open-place card; listed together, its coverage finding is not said again.
  const gapIds=new Set(short.map(x=>x.shift_id)),errorRows=(v.findings as Row[]).filter(f=>f.severity==='error');
  // Open extra cover is optional overtime, not a warning to fix: listed on its own.
  const advisory=(v.findings as Row[]).filter(f=>f.severity!=='error'&&!accepted.has(issueKey(f)));
  const warningRows=advisory.filter(f=>!isExtraCover(f)),cards=(list:Row[])=>causeCards(data,list,finding,done);
  return {gaps,errors:cards(errorRows),otherErrors:cards(errorRows.filter(f=>!(f.shift_id&&!f.person_id&&gapIds.has(f.shift_id)))),
    warnings:cards(warningRows),warningRows,extraCover:advisory.filter(isExtraCover).map(finding)};
}
/** A draft check's cause groups (GET /summary `cause_groups`), read once per check and held for every list that shows
 *  its findings (footer lists, publish checks, auto-schedule results). The published layer has none. */
let causesHeld:{key:string;groups:Promise<FindingCauseGroup[]>;ready?:FindingCauseGroup[];observations?:FindingObservationSummary[]}|null=null;
const causeKey=(data:WorkspaceData)=>`${data.workspace.id}:${data.workspace.revision}:${data.workspace.validation?.fingerprint||''}`;
function loadCauses(data:WorkspaceData,summary?:RotaCauseSummary):Promise<FindingCauseGroup[]>{
  const key=causeKey(data);
  if(causesHeld?.key!==key||summary&&!causesHeld.ready){const held:NonNullable<typeof causesHeld>={key,groups:Promise.resolve([])};causesHeld=held;
    held.groups=data.workspace.id==='published'||!data.workspace.validation?Promise.resolve(held.ready=[])
      :(summary?Promise.resolve(summary):rotaSummary(data.workspace.id)).then(s=>{held.observations=s.observations||[];return held.ready=mergeCauseGroups(s.cause_groups||[],s.repair_tasks);},()=>held.ready=[]);}
  return causesHeld.groups;
}
const heldCauses=(data:WorkspaceData)=>causesHeld?.key===causeKey(data)?causesHeld.ready:undefined;
/** Findings as issue cards; findings that share a cause are ONE card that says why and offers the fix (findings.ts causeIssue). */
function causeCards(data:WorkspaceData,list:Row[],finding:(f:Row)=>Issue,done:()=>void):Issue[]{
  const groups=heldCauses(data)||[],byKey=new Map(list.map(f=>[issueKey(f),f])),used=new Set<string>(),out:Issue[]=[];
  // Only findings that carry the cause themselves: one whose cause sits under it (a person on leave leaving a gap)
  // keeps its own card, with its leave and note.
  // One grouped finding can contain independent observations with different causes; retain each cause and its fixes.
  for(const g of groups){const found=g.finding_keys.map(k=>byKey.get(k)).filter(f=>f?.cause) as Row[];if(!found.length)continue;
    for(const f of found)used.add(issueKey(f));out.push(causeGroupIssue(data,g,found,done));}
  return [...out,...list.filter(f=>!used.has(issueKey(f))).map(finding)];
}
function causeGroupIssue(data:WorkspaceData,g:FindingCauseGroup,found:Row[],done:()=>void,show?:HTMLElement|null):Issue{
  // The group's rules as the server lists them (several when one card stands for the same sessions under two rules).
  const rules=[...new Set([...g.rule_ids,...found.map(f=>f.rule_id).filter(Boolean)])].map(id=>find('policies',id)?.name||'').filter(Boolean);
  const session=(f:Row)=>{const s=f.shift_id&&data.shifts.find(x=>x.id===f.shift_id);
    return s?[s.type_snapshot.name,(s.service_ids as string[]).map(x=>name('services',x)).join(' + '),formatDay(s.start,zone(),true),`${formatTime(s.start,zone())}–${formatTime(s.end,zone())}`].filter(Boolean).join(' · '):'';};
  return causeIssue({...g,count:found.length},found,{who:f=>[f.person_id?name('people',f.person_id):'',session(f)].filter(Boolean).join(' · ')||'Session',rule:rules.length?{name:rules.join(', '),mandatory:g.severity==='error'}:undefined,
    actions:[...fixActions(data,g.fix_hints,done,g.code==='unfilled'?[]:g.rule_ids),show!==undefined?show:button('Show on schedule',()=>{done();showAffected(data,found);},'ghost small'),dutyPeople(data,g,done)]});
}
/** The people the rota check recorded for one duty of this card (its first session's requirement), with what rules each
 *  out: on request, for a coordinator of a checked draft (planning_context.ts). */
function dutyPeople(data:WorkspaceData,g:Pick<FindingCauseGroup,'finding_keys'>,done:()=>void){
  const o=dutyObservation(data,g.finding_keys);
  return o?button('People for this duty',()=>{done();openDutyPeople(data,o);},'ghost small'):null;
}
const dutyObservation=(data:WorkspaceData,keys:string[])=>{const want=new Set(keys);
  return data.workspace.status==='draft'&&permits(CAPABILITY.COORDINATOR)?(causesHeld?.key===causeKey(data)?causesHeld.observations||[]:[])
    .filter(x=>x.shift_id&&want.has(x.finding_key)).sort((a,b)=>String(a.start||'').localeCompare(String(b.start||'')))[0]:undefined;};
function openDutyPeople(data:WorkspaceData,o:FindingObservationSummary){
  requirementEvidence({workspace:data.workspace.id,fingerprint:String(data.current_fingerprint),observation:o,name,zone:zone(),
    fixes:(hints,close)=>fixActions(data,hints,close),check:()=>void command('workspace.validate',{reason:'Routine rota check'},data.workspace.id,data.workspace.revision).then(()=>refresh()).catch(reportError)});
}
/** Where a cause group sits on the schedule: its sessions marked on the calendar, or (a person's quota) its people
 *  marked in the People view, where each row says the breach in words (planner_people.ts). */
function showAffected(data:WorkspaceData,found:Row[]){
  const s=found.map(f=>f.shift_id&&data.shifts.find(x=>x.id===f.shift_id)).find(Boolean);
  if(s){showOnSchedule(s);return;}
  const people=[...new Set(found.map(f=>f.person_id).filter(Boolean))] as string[],first=found.map(f=>f.start).filter(Boolean).sort()[0];
  if(first)planDay=toLocal(first,zone()).slice(0,10);
  setLayoutRange('people',VIEW_RANGE.people);if(view!=='schedule'||schedSubPage())navigate(routeHash('schedule',[schedView,planDay],{edit:data.workspace.id}).slice(1));
  void renderPage().then(()=>requestAnimationFrame(()=>{
    const spots=people.flatMap(id=>[...document.querySelectorAll<HTMLElement>(`.pg-name[data-rk*='"person":"${CSS.escape(id)}"']`)]);
    spots[0]?.scrollIntoView({block:'center',inline:'nearest'});spots[0]?.focus({preventScroll:true});
    for(const n of spots){n.classList.remove('issue-spot');void n.offsetWidth;n.classList.add('issue-spot');}}));
}
/** A cause's fixes from the server's fix hints, each opening the existing form or screen with its context: the first is
 *  the main one. Hints for the same fix in several checked windows (a weekly rule) are one fix over all their days. */
/** `o.except`: an action already offered beside these (a Fix list row's own), skipped except for its series choices;
 *  `o.quiet`: no primary among them (the surface has its own). */
function fixActions(data:WorkspaceData,hints:FindingFixHint[]=[],done:()=>void,rules:string[]=[],o:{except?:string;quiet?:boolean}={}):HTMLElement[]{
  const draft=data.workspace.status==='draft'&&permits(CAPABILITY.COORDINATOR),merged=new Map<string,FindingFixHint>();
  for(const h of hints){const {starts_on,ends_on,shift_ids,...rest}=h.prefill,k=JSON.stringify([h.action,rest]),had=merged.get(k);
    merged.set(k,!had?h:{...had,prefill:{...had.prefill,starts_on:starts_on<had.prefill.starts_on?starts_on:had.prefill.starts_on,ends_on:ends_on>had.prefill.ends_on?ends_on:had.prefill.ends_on,
      shift_ids:[...new Set([...had.prefill.shift_ids||[],...shift_ids||[]])]},matching_series:[...new Map([...had.matching_series||[],...h.matching_series||[]].map(m=>[m.id,m])).values()]});}
  const out:HTMLElement[]=[],people=new Set([...merged.values()].map(h=>h.prefill.person_id).filter(Boolean));
  // One button per label: two hints with the same fix (two people's rotations) are one button (FD-06).
  const go=(label:string,run:()=>void)=>{if(out.length<4&&!out.some(b=>b.textContent===label))out.push(button(label,()=>{done();run();},out.length||o.quiet?'secondary small':'primary small'));};
  for(const h of merged.values()){const p=h.prefill,person=p.person_id?find('people',p.person_id):undefined,whose=person&&people.size>1?' · '+fullNameOf(person):'';
    switch(h.action){
      case 'add_sessions':if(!draft)break;
        // Expanding a series into the draft is a draft change; extending or switching one on edits the series (admin-only).
        for(const m of (h.matching_series||[]).filter(m=>m.action==='expand'||catalogueEditable('recurring_series')).slice(0,2))go(m.action==='expand'?`Add sessions from ${m.name}`:m.action==='extend'?`Extend ${m.name}`:`Switch on ${m.name}`,()=>void useSeries(data,m,p).catch(reportError));
        // Recurring sessions are admin-only: others add the whole pattern to this rota's draft.
        if(o.except!=='add_sessions')go(`Add ${causeWhat([h])||'matching'} sessions${catalogueEditable('recurring_series')?'':' to this rota'}`,()=>void newSeries(data,p).catch(reportError));break;
      case 'open_eligibility':if(o.except==='open_eligibility')break;if(person)go('Open eligibility'+whose,()=>personDetails(person));else go('Open People',()=>navigate('people'));break;
      case 'open_leave':if(person)go('Open leave'+whose,()=>personDetails(person,undefined,'leave'));break;
      case 'open_rotation':go('Open rotations',()=>navigate('programmes/rotations'));break;
      // The rota's own rules dock beside its schedule: from the Fix list, the schedule opens first.
      // The rota's rules panel beside its schedule (at the finding's rule): from the Fix list, the schedule opens first.
      case 'open_rota':go('Open rota rules',()=>openRotaPanel(data.workspace.id,p.rule_id||''));break;
      case 'raise_capacity':if(p.rule_id)go('Raise the limit',()=>void openRuleSource(p.rule_id!,p.person_id||undefined).catch(reportError));break;
      // A proposal waiting is reviewed from its one footer pill and the Auto-schedule button, never replaced unseen by
      // running again (G-13): no card repeats it.
      case 'run_auto_schedule':if(draft&&!proposalWaiting(data))go('Run auto-schedule',()=>{schedulingSettings.open=true;workspaceId=data.workspace.id;draftNone=false;switchView('week');
        navigate(routeHash('schedule',[schedView,planDay],{edit:data.workspace.id}).slice(1));});break;
      case 'review_assignments':go('Review assignments',()=>openIssues(data.workspace.id));break;
    }
  }
  // No fix the server names (a limit everyone has reached): the rule itself, where a change is proposed and approved
  // as usual. Nothing is raised, granted or accepted from here.
  if(!out.length&&!o.except)for(const id of rules.slice(0,2)){const rule=find('policies',id)?.name;if(rule)go(`Open ${rule}`,()=>void openRuleSource(id).catch(reportError));}
  return out;
}
const freshRota=(data:WorkspaceData)=>rotaWhole(data.workspace.id);
/** "Add UCC sessions": the recurring-session form filled in from the cause (duty type, clinic, place, days, times and
 *  dates); once created, its sessions go into this draft through the usual reviewed Add recurring sessions. Someone
 *  who may not change recurring sessions (a coordinator) adds the same pattern to this rota only (patternSessions). */
async function newSeries(data:WorkspaceData,p:FindingFixHint['prefill']){
  await ensureCatalogues();
  if(!catalogueEditable('recurring_series')){patternSessions(data,p);return;}
  const before=new Set(rows('recurring_series').map(r=>r.id));
  catalogueForm(ctx(),'recurring_series',undefined,{assignment_type_id:p.assignment_type_ids?.[0]||'',location_id:p.location_id||'',starts_on:p.starts_on,ends_on:p.ends_on,
    definition:{service_ids:p.service_ids||[],days:(p.weekdays||[]).map(weekday=>({weekday,...(p.start_time?{start_time:p.start_time}:{}),...(p.end_time?{end_time:p.end_time}:{})})),default_person_ids:[]}},
    {note:`After you create it, its sessions can be added to ${data.workspace.name}.`,run:async()=>{await ensureCatalogues();
      const made=rows('recurring_series').find(r=>!before.has(r.id));if(made)seriesMaterialise(await freshRota(data),made.id);}});
}
/** "Add UCC sessions to this rota" (someone who may not change recurring sessions): the whole pattern of the cause, its
 *  days and times across its dates in this rota, added to the draft in ONE bulk change (one transaction, one Undo).
 *  People are optional: empty sessions are saved for auto-schedule to fill, and the problems the save brings (the
 *  server's `warnings`, e.g. staffing gaps) are shown right after it as the same explained cards. Draft only. */
function patternSessions(data:WorkspaceData,p:FindingFixHint['prefill']){
  const w=data.workspace,zoneName=zone(),from=p.starts_on>w.starts_on?p.starts_on:w.starts_on,until=p.ends_on<w.ends_on?p.ends_on:w.ends_on;
  const timing=(id:string)=>find('assignment_types',id)?.definition?.default_timing as {start_time:string;end_time:string}|undefined;
  formDialog(`Add ${causeWhat([{action:'add_sessions',prefill:p}])||'matching'} sessions to this rota`,
    `Draft only, as one change with one Undo: ${spanLabel(from+'T00:00:00Z',until+'T00:00:00Z','UTC')}. Recurring sessions stay as they are.`,form=>{
    const typ=select('Duty type',choices(rows('assignment_types')),p.assignment_type_ids?.[0]||''),place=select('Location',choices(rows('locations')),p.location_id||'');
    const clinics=multi('Clinics and services',choices(rows('services')),p.service_ids||[]);
    const t=timing(typ.input.value),begin=field('Start','text',p.start_time||t?.start_time||'08:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]',inputmode:'numeric',maxlength:5});
    const finish=field('End','text',p.end_time||t?.end_time||'12:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00',inputmode:'numeric',maxlength:5});
    const days=weekOrder().map(i=>({i,box:check(WEEKDAYS[i],!!p.weekdays?.includes(i))}));
    const who=multi('People on each session (optional)',peopleChoices(ctx()),[],'Auto-schedule fills them');
    const dates=()=>{const out:string[]=[];for(let d=from;d<until;d=addDays(d,1))if(days.some(x=>x.box.input.checked&&x.i===(new Date(d+'T12:00:00Z').getUTCDay()+6)%7))out.push(d);return out;};
    const said=el('p',{class:'span-2','aria-live':'polite'});
    const sync=()=>{const list=dates();days[0].box.input.setCustomValidity(list.length?'':'Tick at least one day in this period.');
      said.textContent=list.length?`${plural(list.length,'session')}: ${list.map(d=>formatDay(d,zoneName,true)).join(', ')}`:'No sessions yet: tick the days they run.';};
    form.addEventListener('change',sync);sync();
    for(const f of [clinics.wrap,who.wrap,said])f.classList.add('span-2');
    form.append(typ.wrap,place.wrap,clinics.wrap,el('div',{class:'span-2',role:'group','aria-label':'Days'},...days.map(x=>x.box.wrap)),begin.wrap,finish.wrap,who.wrap,said,
      note('Anyone chosen works every session. Leave it empty to let auto-schedule share the sessions out.','info'));
    return async()=>{
      const list=dates(),ids=who.values();
      // Local wall time → instant in the browser (one request per date was ~60 for a month); a clock change (a skipped
      // or repeated hour) asks the server, whose rule decides it.
      const at=(d:string,hm:string)=>{const local=hm==='24:00'?addDays(d,1)+'T00:00':`${d}T${hm}`,guess=Date.parse(local+':00Z');
        const t=guess-(Date.parse(toLocal(new Date(guess).toISOString(),zoneName)+':00Z')-guess),iso=new Date(t).toISOString();
        return toLocal(iso,zoneName)===local&&toLocal(new Date(t-3600000).toISOString(),zoneName)!==local&&toLocal(new Date(t+3600000).toISOString(),zoneName)!==local?Promise.resolve(iso):instant(local);};
      const items=await Promise.all(list.map(async d=>{const a=await at(d,begin.input.value),b=await at(finish.input.value<=begin.input.value?addDays(d,1):d,finish.input.value);
        return {item_kind:'shift.create',expected_revision:w.revision,payload:{workspace_id:w.id,assignment_type_id:typ.input.value,location_id:place.input.value,service_ids:clinics.values(),
          start:a,end:b,participants:ids.map(person_id=>({person_id,start:a,end:b}))}};}));
      const result=await command<Row>('bulk.apply',{items,reason:`Add sessions for a rule breach (${list.length})`});
      const found=((result.warnings||[]) as Row[]).filter(f=>f.workspace_id===w.id);
      const said=`${plural(list.length,'session')} added to ${w.name}`;
      // New issues: a warning, never a green all-clear; it keeps the one Undo of the whole change.
      if(!found.length)notify(said+'.');
      else{const undo=takeUndo();notify(`${said} · ${plural(found.length,'new issue')} to resolve.`,'warning',undo?{label:'Undo',run:()=>undo.run().then(()=>notify(undo.done||'Undone. The previous values are back.'),e=>notify((e as Error).message,'error',undefined,e))}:undefined);}
      await refresh();
      if(found.length)void savedFindings(data,found,list.length).catch(reportError);
    };
  },'Add sessions');
}
/** What a save that went through still left to fix (a bulk change's `warnings`): never an all-clear, the same explained
 *  cards as the footer lists, with their fixes (e.g. Run auto-schedule for empty sessions). */
async function savedFindings(data:WorkspaceData,found:Row[],added:number){
  const fresh=await freshRota(data);await loadCauses(fresh);
  const byId=new Map(fresh.shifts.map(s=>[s.id,s]));
  dockPanel({key:'new-issues',title:`${plural(found.length,'new issue')} to resolve`,sub:`${plural(added,'session')} saved in ${fresh.workspace.name}. Saved is not the same as ready: resolve these before review.`,build:({body,close})=>{
    const cards=causeCards(fresh,found,f=>{const s=f.shift_id?byId.get(f.shift_id):undefined;return findingCard(f,s,findingActions(fresh,f,s,close));},close);
    body.append(issueList(cards,{label:'New issues',done:close}));}});
}
/** A matching recurring session: its sessions added to this draft, or first extended to the dates or switched on. */
async function useSeries(data:WorkspaceData,m:NonNullable<FindingFixHint['matching_series']>[number],p:FindingFixHint['prefill']){
  await ensureCatalogues();
  const s=find('recurring_series',m.id);
  if(m.action==='expand'||!s){seriesMaterialise(await freshRota(data),m.id);return;}
  // Extend only what falls short of the breach's dates (finding_causes.py): an earlier start, a later end when it has
  // one (an open-ended series keeps no end); switching on keeps its dates.
  const extend=m.action==='extend';
  catalogueForm(ctx(),'recurring_series',s,{...(extend&&s.starts_on>p.starts_on?{starts_on:p.starts_on}:{}),...(extend&&s.ends_on&&s.ends_on<p.ends_on?{ends_on:p.ends_on}:{}),active:true},
    {note:`After you save it, its sessions can be added to ${data.workspace.name}.`,run:async()=>seriesMaterialise(await freshRota(data),m.id)});
}
/** What can be done about one finding, the same wherever it is listed: see the session, take the person off it or pick
 *  someone else in their place (a blocking problem of a person on an editable draft session), its details, or ask for
 *  an exception. `done` closes the list the card sits in first. */
function findingActions(data:WorkspaceData,f:Row,s:Row|undefined,done:()=>void):Issue['actions']{
  const draft=data.workspace.status==='draft'&&permits(CAPABILITY.COORDINATOR),blocking=f.severity==='error';
  const a=draft&&blocking&&s&&!s.locked&&!s.cancelled&&f.person_id?(s.participants as Row[]).find(p=>p.person_id===f.person_id):undefined;
  const who=f.person_id?name('people',f.person_id):'';
  return [...fixActions(data,f.fix_hints,done),s?button('Show on schedule',()=>{done();showOnSchedule(s);},'secondary small'):null,
    a?button('Pick someone else…',()=>{done();queueFill(data.workspace.id,s!.id,a.id);toCalendar(s!);void renderPage();},'secondary small',{'aria-label':`Pick someone else instead of ${who}`}):null,
    a?button('Remove from session',async()=>{done();await removeAssignment(data.workspace,s!,a.id);await refresh();},'ghost small',{'aria-label':`Remove ${who} from this session`}):null,
    s?button('Details',()=>{done();if(!openSession(s))readOnlySession(s);},'ghost small'):null,
    draft&&blocking&&f.rule_id&&f.person_id&&(s||f.start)?exceptionButton(f.rule_id,f.person_id,s?.start||f.start):null,
    // A double booking: cancel either session, here (Undo in the message).
    ...(f.detail?.overlap?cancelButtons(f.detail.overlap as Clash,async()=>{done();await refresh();}):[])];
}
/** Schedule sub-pages that are lists, not the planner grid: the Fix list and Compare your rota with Rota. */
/** Score checking is off by default (department.score_check_enabled): its entry points and AI provider settings stay hidden. */
const scoreCheckOn=()=>(me?.department as Row|undefined)?.score_check_enabled===true;
const openScoreCheck=()=>navigate(routeHash('schedule',['benchmark','upload']).slice(1));
/** Schedule › Compare your rota with Rota (score_check.ts): its address is #schedule/benchmark/<step>?import|draft=… */
function scoreCheckHooks(){
  const step=(['upload','check','compare'].includes(routeParts[1]||'')?routeParts[1]:'upload') as ScoreStep,mine=renderSerial;
  const kinds:Record<string,string>={person:'people',duty_code:'assignment_types',clinic:'services',location:'locations',cell:'people',session:'assignment_types'};
  return {step,query:routeQuery,zone:zone(),canEdit:permits(CAPABILITY.COORDINATOR),canRead:permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR),canAddMissing:may('score_check.add_missing'),
    go:(to:ScoreStep,q:Record<string,string>)=>navigate(routeHash('schedule',['benchmark',to],q).slice(1)),leave:()=>navigate('schedule'),
    setLeave:(dirty:(()=>boolean|string)|null)=>{if(mine===renderSerial)leaveCheck=dirty;},
    choices:(kind:string)=>choices(rows(kinds[kind]||'people'),(kinds[kind]||'people')==='people'),
    openIssues,openRules:(id:string)=>openRotaPanel(id),openScoring:()=>navigate('configuration/auto_schedule'),
    submit:(id:string)=>void rotaWhole(id).then(publishDialog).catch(reportError),
    // Rota's draft of the same sessions (score_check.auto), opened with Auto-schedule ready to run (the Planner's own flow).
    autoSchedule:(id:string)=>void rotaMeta(id).then(d=>autoSameSessions(id,d.workspace.revision))
      .then(r=>rotaMeta(r.workspace.id)).then(d=>openPaletteRota(d.workspace,true)).catch(reportError),
    // Re-run: Rota's existing draft opens with the Planner's own Auto-schedule (score_check.auto only copies an imported rota).
    rerun:(id:string)=>void rotaMeta(id).then(d=>openPaletteRota(d.workspace,true)).catch(reportError)};
}
const schedSubPage=(parts=routeParts)=>parts[0]==='issues'||parts[0]==='benchmark';
/** FIXLIST-UX (direction A; coordinator decision 7 Nov 2026 under D80): on a desktop the Fix list is the Planner's
 *  docked side panel, so a fix happens beside the schedule: every must-fix cause open, the one in focus with its
 *  sessions as date chips (each rings its session) and Assign in place, moving on to the next open session after each;
 *  Could improve collapsed. The footer, Rota tools, Home, a marker's Fix and the run card all open it. Phones, and
 *  readers who cannot plan, keep the page (#schedule/issues): the same rows, with Assign (D10 exception, this flow). */
type FixFocus={rota:string;cause?:string;shift?:string;/** Only these causes (an issue list's Open all), until Show all. */only?:string[];
  /** The person chose this session (its chip, the row's Assign, a marker's Fix, the move on after their Assign): its
   *  candidates load at once. Otherwise (the panel opened, an Open all) the box asks first: fill-options only when asked. */chosen?:boolean;
  done:Map<string,{cause:string;who:string}>};
let fixFocus:FixFocus|null=null,fixPanelRota='',fixDraws=0,fixRefreshing=false;
const fixCache=new Map<string,{key:string;list:FixList}>(),fixChipsOpen=new Set<string>();
const planOnDesk=()=>!phoneQuery.matches&&permits(CAPABILITY.COORDINATOR);
const openIssuesPage=(workspace:string)=>navigate(routeHash('schedule',['issues'],{draft:workspace}).slice(1));
function openIssues(workspace:string,focus:{cause?:string;shift?:string;only?:string[];chosen?:boolean}={}){
  const w=scheduleCache.drafts.get(workspace)?.workspace;
  if(!planOnDesk()||w&&w.status!=='draft'){openIssuesPage(workspace);return;}
  focusOn(workspace,{...focus,chosen:focus.chosen??!!focus.shift});fixPanelRota=workspace;
  if(view==='schedule'&&!schedSubPage()&&workspaceId===workspace&&document.querySelector('#main .planner-body')){void drawFixPanel(true);return;}
  workspaceId=workspace;draftNone=false;navigate(routeHash('schedule',[schedView,planDay],{edit:workspace}).slice(1));
}
/** Where an issue list's items go (issue_marker.ts issueList, the one list): an item shows its session (marked now when
 *  drawn; otherwise the schedule moves to its day and marks it once drawn, as a ?shift= link does), and Open all opens
 *  the Fix list panel at exactly the causes holding those findings. */
setIssueNav({
  go:i=>{if(!i.shift)return;
    if(document.querySelector(`#main [data-rk*='"shift":"${CSS.escape(i.shift)}"']`)){markShift(i.shift);return;}
    const s=view==='schedule'&&!schedSubPage()?scheduleCache.drafts.get(workspaceId)?.shifts.find(x=>x.id===i.shift):undefined;
    if(!s){markShift(i.shift);return;}  // says it is not shown with this view, search or filters
    toCalendar(s);routeShift=i.shift;void redraw();},
  canFix:xs=>xs.some(i=>!!i.key)&&view==='schedule'&&!schedSubPage()&&planOnDesk()&&(scheduleCache.drafts.get(workspaceId)?.workspace||(plannerRota?.id===workspaceId?plannerRota:null))?.status==='draft',  // the rota drawn, whole or its window first
  fixList:xs=>{const id=workspaceId,keys=new Set(xs.map(i=>i.key).filter((k):k is string=>!!k));
    void fixListFor(id,scheduleCache.drafts.get(id)||null).then(l=>{const only=l.causes.filter(c=>c.finding_keys.some(k=>keys.has(k))).map(c=>c.id);
      const first=xs.find(i=>i.shift&&l.causes.some(c=>only.includes(c.id)&&causeShifts(c).includes(i.shift!)));
      openIssues(id,{only,cause:only[0],shift:first?.shift,chosen:false});}).catch(reportError);}});
/** The panel's focus: a cause, and its session in focus; `done`: sessions an edit's fix_list_delta took out of a cause
 *  (the server's word that they are fixed), kept as ✓ chips on that cause with who was assigned. */
function focusOn(rota:string,f:{cause?:string;shift?:string;only?:string[];chosen?:boolean}){fixFocus={rota,done:fixFocus?.rota===rota?fixFocus.done:new Map(),...f};}
const causeOfShift=(l:FixList,shift:string)=>l.causes.find(c=>c.level==='must'&&causeShifts(c).includes(shift))||l.causes.find(c=>causeShifts(c).includes(shift));
/** A cause one assignment fixes: the server's best fix (fix_list_facts.ordered_fixes) is Assign and available. */
const assignable=(c:FixCause)=>!!c.fixes?.[0]&&ASSIGN_KINDS.has(c.fixes[0].kind)&&c.fixes[0].available!==false;
/** Sessions the server took out of this cause (✓ chips), unless a later check lists them in it again. */
const doneOf=(c:FixCause,data:WorkspaceData)=>{const d=fixFocus?.rota===data.workspace.id?fixFocus.done:null,open=new Set(causeShifts(c));
  return d?data.shifts.filter(s=>d.get(s.id)?.cause===c.id&&!open.has(s.id)):[];};
/** FIXLIST-BACKEND §5: an edit's reply carries fix_list_delta; it is applied to the Fix list held for that rota at once
 *  (the server's truth, no read), and the sessions it took out of a cause become that cause's ✓ chips. The complete
 *  check still follows; its result is read when it lands. */
window.addEventListener('rota-saved',e=>applyFixReply((e as CustomEvent).detail?.result));
/** An edit's reply (this page's own, or another person's carried on its pulse): its fix_list_delta updates the Fix list held. */
function applyFixReply(r:Row|null|undefined){const d=fixDeltaOf(r),id:string|undefined=r?.workspace?.id;
  if(!d?.complete||!id)return;const was=fixCache.get(id)?.list||lastFixList(id);if(!was)return;
  // An Undo is one more edit with its own delta (maintenance): the same path, so its sessions come back into their causes.
  const now=applyFixDelta(was,d),still=new Map(now.causes.map(c=>[c.id,new Set(causeShifts(c))]));
  if(fixFocus?.rota===id)for(const c of was.causes)for(const sid of causeShifts(c))if(!still.get(c.id)?.has(sid))
    fixFocus.done.set(sid,{cause:c.id,who:fixFocus.done.get(sid)?.who||''});
  settleDone(id,now);
  // maintenance (one source): keyed exactly as fixListFor and heldFixSummary key the draft this reply patched (rota-saved's
  // patchDraft ran first), so the footer, its popover and the panel all read this list; a complete check that lands
  // later has another stamp, so it is read, never taken for this edit's list.
  const patched=scheduleCache.drafts.get(id)?.workspace;
  fixCache.set(id,{key:patched?fixKey(patched.revision,patched.validation as Row|undefined)
    :fixKey((r as Row)?.workspace?.revision,((r as Row)?.validation||(r as Row)?.workspace?.validation) as Row|undefined),list:now});}
/** Another person's edit on its pulse (remoteEdit), applied as this page's own reply is: patchDraft (only at the held
 *  draft's base revision) and the Fix list delta. True only when the held draft now is that edit's: drawn from memory. */
function applyRemoteEdit(r:Row):boolean{
  const id=String(r.workspace.id);if(!scheduleCache.drafts.has(id)||!patchDraft(r,''))return false;
  if(scheduleCache.drafts.get(id)?.workspace.revision!==r.workspace.revision)return false;
  applyFixReply(r);return true;
}
/** ✓ chips last only while the server says their session left its cause: a delta (an Undo's too) or a full read that
 *  lists the session in a cause again drops it, so no local list of fixes outlives an edit (maintenance). */
function settleDone(id:string,l:FixList){
  if(fixFocus?.rota!==id||!fixFocus.done.size)return;
  const open=new Set(l.causes.flatMap(c=>causeShifts(c)));
  for(const sid of [...fixFocus.done.keys()])if(open.has(sid))fixFocus.done.delete(sid);
}
/** A Fix list's cache key: the rota's revision and its check's stamp (evaluated_at, and `scope` while only an edit's
 *  sessions were checked). */
const fixKey=(revision:unknown,v:Row|null|undefined)=>JSON.stringify([revision,v?.evaluated_at??null,v?.scope??null]);
/** The Fix list summary held for the rota as it is now (same key as fixListFor), or null: the footer's counts. */
const heldFixSummary=(id:string,data:WorkspaceData|null)=>{const h=fixCache.get(id);
  return h&&data&&h.key===fixKey(data.workspace.revision,data.workspace.validation as Row|undefined)?h.list.summary:null;};
/** The list a footer was last drawn from, per rota: a panel read that changes the counts draws the footer again, once. */
const footerFrom=new Map<string,FixSummary|null>(),footerRedrawn=new WeakSet<FixList>();
const sessionsOf=(c:FixCause,data:WorkspaceData)=>{const ids=new Set(causeShifts(c));return data.shifts.filter(s=>ids.has(s.id)).sort((a,b)=>String(a.start).localeCompare(String(b.start)));};
const drawnSession=(id:string)=>document.querySelector<HTMLElement>(`#main [data-session~="${CSS.escape(id)}"]`);
/** The cause in focus and its session in focus (the one asked for, else the first open one on screen, else the first). */
function focusedCause(l:FixList,data:WorkspaceData|null):{cause:FixCause;shift:Row|null}|null{
  const f=fixFocus;if(!f||!data||f.rota!==data.workspace.id)return null;
  const cause=(f.cause&&l.causes.find(c=>c.id===f.cause))||(f.shift&&causeOfShift(l,f.shift))||l.causes.find(c=>c.level==='must'&&assignable(c));
  if(!cause)return null;
  const open=sessionsOf(cause,data);
  return {cause,shift:open.find(s=>s.id===f.shift)||open.find(s=>!!drawnSession(s.id))||open[0]||null};
}
/** The cause's sessions ringed on the schedule: the one in focus solid, the others dashed (a ring, not colour alone). */
function ringCause(at:{cause:FixCause;shift:Row|null}|null,scroll=false){
  for(const n of document.querySelectorAll('#main .cause-near'))n.classList.remove('cause-near');
  if(!document.querySelector('.cause-bar'))for(const n of document.querySelectorAll('#main .cause-ring'))n.classList.remove('cause-ring');
  if(!at)return;
  for(const id of causeShifts(at.cause))for(const n of document.querySelectorAll(`#main [data-session~="${CSS.escape(id)}"]`))n.classList.add(id===at.shift?.id?'cause-ring':'cause-near');
  if(scroll&&at.shift)drawnSession(at.shift.id)?.scrollIntoView({block:'nearest',inline:'nearest'});
}
const chipDay=(s:Row)=>formatDay(s.start,zone()).split(' ').slice(0,2).join(' ');  // "Fri 11": the month is in its name and tip
/** Where a cause is: its sessions (open, and those its edits fixed) as the one session-chip model (issue_marker.ts
 *  sessionChips), the one in focus pressed; "+N more" stays open across redraws (fixChipsOpen). */
function causeChips(c:FixCause,data:WorkspaceData,on:string,go:(s:Row)=>void,again:()=>void):HTMLElement|null{
  const all=[...sessionsOf(c,data),...doneOf(c,data)].sort((a,b)=>String(a.start).localeCompare(String(b.start)));
  const fixed=(s:Row)=>fixFocus?.rota===data.workspace.id?fixFocus.done.get(s.id):undefined;
  // Two or more sessions on one day carry their time ("Sun 11 · 08:00"), so no two chips read the same (F7).
  const labels=dayLabels(all.map(s=>({day:chipDay(s),time:formatTime(s.start,zone())})));
  return sessionChips(all.map((s,n):SessionChip=>({label:labels[n],full:`${formatDay(s.start,zone(),true)} ${formatTime(s.start,zone())}, ${s.type_snapshot?.name||'Session'}`,
      pressed:s.id===on,...(fixed(s)?{done:{who:fixed(s)!.who}}:{}),go:()=>go(s)})),
    {label:c.where?.label?`Sessions: ${c.where.label}`:'Sessions',expanded:fixChipsOpen.has(c.id),onExpand:()=>{fixChipsOpen.add(c.id);again();}});
}
/** The fix in place for the session in focus: its day on one line, then who can work it (inlineFill) and Assign. After
 *  a save (the edit's own message holds Undo) the focus moves to the cause's next open session; the rota checks itself. */
function fillBox(c:FixCause,s:Row,data:WorkspaceData,again:()=>void):HTMLElement{
  const id=data.workspace.id,key=c.session_findings?.[s.id]||'',issue=key?{key,title:c.sentence.replace(/[.\s]+$/,''),severity:c.level==='must'?'error':'warning'}:undefined;
  return el('div',{class:'fix-fill-box'},el('div',{class:'fix-fill-head'},el('strong',{class:'fix-fill-day'},formatDay(s.start,zone())),
      el('span',{class:'muted truncate'},`${s.type_snapshot?.name||'Session'} · ${formatTime(s.start,zone())}–${formatTime(s.end,zone())}`)),
    inlineFill({data,shift:s,issue,code:pid=>shortCode(find('people',pid)),
      ask:!!fixFocus?.chosen&&(!fixFocus.shift||fixFocus.shift===s.id),asked:()=>{if(fixFocus?.rota===id)focusOn(id,{...fixFocus,cause:c.id,shift:s.id,chosen:true});},
      more:()=>{queueFill(id,s.id,undefined,issue);toCalendar(s);void redraw();},
      // Refused because the rota moved on: forget the draft and Fix list held, and read both again.
      stale:async()=>{scheduleCache.drafts.delete(id);fixCache.delete(id);await refresh();},
      // The reply's fix_list_delta has already said whether the session is fixed (rota-saved, above): name who on its ✓
      // chip, then stay on it while it is still open, else move to the cause's next open session.
      done:async who=>{const fixed=fixFocus?.done.get(s.id);if(fixed)fixed.who=who;
        const now=fixCache.get(id)?.list.causes.find(x=>x.id===c.id),open=now?sessionsOf(now,data):[];
        // The move on after an Assign is the person's (they are fixing these): its candidates load at once (LEAD-UIUX).
        focusOn(id,{cause:c.id,shift:(open.find(x=>x.id===s.id)||open.find(x=>x.start>s.start)||open[0])?.id,chosen:true});
        // The reply's delta is held already: show its ✓ and the next session now, not after the redraw below (QA cycle 3 maintenance:
        // under load, or with the rota's check in flight, that redraw can take long).
        again();await refresh();}}));
}
/** What the Fix list's rows do in place for a coordinator of the draft (the panel and the page alike). */
function fixFocusHooks(id:string,data:WorkspaceData|null,l:FixList,again:()=>void,panel:boolean):Partial<FixHooks>{
  if(!data||data.workspace.status!=='draft'||data.workspace.archived_at||!permits(CAPABILITY.COORDINATOR))return {};
  const at=focusedCause(l,data);
  const go=(c:FixCause,s:Row)=>{focusOn(id,{cause:c.id,shift:s.id,chosen:true});
    // A session in another week: the schedule moves there; the panel follows it (it is drawn again with the page).
    if(panel&&!drawnSession(s.id)){toCalendar(s);void redraw();return;}
    again();if(panel)ringCause(focusedCause(l,data),true);};
  return {chips:c=>causeChips(c,data,at?.cause.id===c.id?at.shift?.id||'':'',s=>go(c,s),again),
    assign:c=>assignable(c)&&at?.cause.id!==c.id?()=>{const sid=String(c.fixes?.[0]?.scope?.shift_id||''),s=sessionsOf(c,data).find(x=>x.id===sid)||sessionsOf(c,data)[0];if(s)go(c,s);}:null,
    fill:c=>at?.cause.id===c.id&&at.shift&&assignable(c)&&!at.shift.locked&&!isClosed(at.shift)?fillBox(c,at.shift,data,again):null};
}
/** A rota's whole Fix list as held (the panel and the page): read again only when the rota or its check changed. A list
 *  an edit's fix_list_delta brought up to date counts as read for that revision, so an edit never costs a read; the
 *  complete check's result is read when it lands. */
/** A Fix list read in flight per rota, with the key it was asked for. */
const fixReading=new Map<string,{key:string;read:Promise<FixList>}>();
async function fixListFor(id:string,data:WorkspaceData|null,force=false):Promise<FixList>{
  // The rota's revision and its check's stamp (evaluated_at, and `scope` while only an edit's sessions were checked): the
  // complete check landing changes it; an edit's own revision is the delta's (never the client's checking flag).
  const key=fixKey(data?.workspace.revision,data?.workspace.validation as Row|undefined);
  const now=fixCache.get(id);if(now?.key===key&&!force)return now.list;
  // Two draws asking at once share one read (the capture saw the second send an ETag the first had replaced).
  const pending=fixReading.get(id);if(pending?.key===key&&!force)return pending.read;
  const read=readFixList(id).then(l=>{fixCache.set(id,{key,list:l});settleDone(id,l);return l;})
    .finally(()=>{if(fixReading.get(id)?.read===read)fixReading.delete(id);});
  fixReading.set(id,{key,read});return read;
}
/** Where the focus is inside `root` (its row's cause, the control's name), and a function that puts it back on the same
 *  control in a redrawn `root`: the chip, the Assign, the Show it was on. Nothing when the focus was elsewhere. */
function focusIn(root:HTMLElement|null):(to:HTMLElement|null)=>void{
  const at=document.activeElement as HTMLElement|null;
  if(!root||!at||!root.contains(at))return ()=>{};
  const cause=at.closest<HTMLElement>('[data-cause]')?.dataset.cause||'',name=at.getAttribute('aria-label')||at.textContent?.trim()||'';
  return to=>{if(!to||to.contains(document.activeElement))return;
    const scope=cause?to.querySelector<HTMLElement>(`[data-cause="${CSS.escape(cause)}"]`):to;
    const same=[...(scope||to).querySelectorAll<HTMLElement>('button,a[href],[tabindex]')].find(n=>(n.getAttribute('aria-label')||n.textContent?.trim()||'')===name);
    (same||scope?.querySelector<HTMLElement>('button')||null)?.focus({preventScroll:true});};
}
/** Opens (or draws again, in place) the Fix list panel of `fixPanelRota` beside the Planner. Read again only when the
 *  rota or its check changed; a recheck in flight keeps the rows, with "Checking…". */
async function drawFixPanel(force=false){
  const id=fixPanelRota,data=scheduleCache.drafts.get(id)||null,n=++fixDraws;
  if(!id||workspaceId!==id||!planOnDesk()){if(document.querySelector('.dock-panel.fix-side'))closeDock();return;}
  let l:FixList;try{l=await fixListFor(id,data,force);}catch(e){reportError(e,couldntLoad('the Fix list')+' ');return;}
  if(n!==fixDraws||fixPanelRota!==id)return;
  // The footer said other counts (drawn before this read): draw it again from this list, once per list.
  {const f=footerFrom.get(id);if(f!==undefined&&(f?.must!==l.summary.must||f?.could!==l.summary.could)&&!footerRedrawn.has(l)){footerRedrawn.add(l);void redraw();}}
  const list=l;
  const shown=!!document.querySelector('.dock-panel.fix-side');
  const held=shown?focusIn(document.querySelector<HTMLElement>('.dock-panel.fix-side .dock-body')):null;
  fixRefreshing=shown;
  try{dockPanel({key:'fix-list',className:'fix-side',title:'Fix list',width:440,refresh:shown,
    onClose:leaving=>{if(fixRefreshing)return;stopAsking('fix');if(!leaving){fixPanelRota='';fixFocus=null;}ringCause(null);},
    // Painted from what is held now (an edit's delta lands between reads; the draft its reply patched), not this read.
    build:({body})=>{const paint=()=>{const now=fixCache.get(id)?.list||list,d=scheduleCache.drafts.get(id)||data,keep=focusIn(body);
        const h:FixHooks={...fixHooks(id,d),...fixFocusHooks(id,d,now,paint,true)};
        // An issue list's Open all: exactly its causes, said with how many of the rota's, and Show all.
        const only=fixFocus?.rota===id&&fixFocus.only?new Set(fixFocus.only):null,shown=only?{...now,causes:now.causes.filter(c=>only.has(c.id))}:now;
        body.replaceChildren(...(only?[el('p',{class:'fix-only'},`Showing ${shown.causes.length} of ${now.causes.length}`,
            button('Show all',()=>{if(fixFocus)fixFocus.only=undefined;paint();},'link-button'))]:[]),
          ...fixPanelNodes({...shown,summary:{...now.summary,...(autoChecking&&workspaceId===id?{checking:!checkStalled(),checking_words:checkSaid()}:{})}},h,readyStep(d?.workspace,d)));
        ringCause(focusedCause(now,d));keep(body);};
      paint();}});}
  finally{fixRefreshing=false;}
  // A redraw in place never takes the focus from where it was in the panel (dockPanel's refresh puts it on the title).
  held?.(document.querySelector<HTMLElement>('.dock-panel.fix-side .dock-body'));
}
/** R3: the Schedule in focus on one cause. "Show on schedule" on a cause opens its first session's week with the cause
 *  bar ("k of n · sentence · ‹ › · action · ×") under the planner bar; its sessions are ringed and the others dimmed
 *  (opacity and a ring: colour is never the only signal). J / K step to the next / previous cause, Esc leaves. */
let causeFocus:{id:string;causes:FixCause[];at:number}|null=null;
const causeShifts=(c:FixCause)=>[...new Set([...(c.shift_ids||[]),...(c.action?.scope?.shift_ids||[])])];
function focusCause(id:string,causes:FixCause[],at:number){
  const list=causes.filter(c=>causeShifts(c).length);if(!list.length)return;
  const i=Math.max(0,list.indexOf(causes[at])),c=list[i];causeFocus={id,causes:list,at:i};
  const first=scheduleCache.drafts.get(id)?.shifts.filter(s=>causeShifts(c).includes(s.id)).sort((a,b)=>a.start.localeCompare(b.start))[0];
  workspaceId=id;draftNone=false;if(first)toCalendar(first);else if(c.action?.scope?.starts_on)planDay=c.action.scope.starts_on;
  closeDialogs();navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));
}
function drawCauseFocus(main:HTMLElement){
  main.querySelector('.cause-bar')?.remove();main.classList.remove('cause-focus');
  for(const n of main.querySelectorAll('.cause-ring'))n.classList.remove('cause-ring');
  const f=causeFocus;if(!f||view!=='schedule'||schedSubPage()||f.id!==workspaceId)return;
  const c=f.causes[f.at],step=(d:number)=>focusCause(f.id,f.causes,(f.at+d+f.causes.length)%f.causes.length);
  const bar=el('div',{class:'cause-bar',role:'region','aria-label':'Cause in focus'},el('span',{class:'fix-mark '+c.level,'aria-hidden':'true'},c.level==='must'?icon('alert'):el('i',{})),
    el('span',{class:'pos'},`${f.at+1} of ${f.causes.length}`),el('strong',{},c.sentence),
    iconButton('Previous cause (K)','chevronUp',()=>step(-1),{class:'bare'}),iconButton('Next cause (J)','chevronDown',()=>step(1),{class:'bare'}),
    c.action&&permits(CAPABILITY.COORDINATOR)?button(actionLabel(c),()=>fixAction(c,f.id,scheduleCache.drafts.get(f.id)||null),'secondary small'):'',
    iconButton('Leave focus (Esc)','close',()=>{causeFocus=null;drawCauseFocus(main);},{class:'bare'}));
  (main.querySelector('.planner-main')||main).prepend(bar);main.classList.add('cause-focus');
  for(const id of causeShifts(c))for(const n of main.querySelectorAll(`[data-session~="${CSS.escape(id)}"]`))n.classList.add('cause-ring');
}
document.addEventListener('keydown',e=>{if(!causeFocus||e.ctrlKey||e.metaKey||e.altKey||(e.target as HTMLElement)?.closest?.('input,textarea,select,[contenteditable=true],dialog'))return;
  const k=e.key.toLowerCase(),f=causeFocus;
  if(k==='j'||k==='k'){e.preventDefault();focusCause(f.id,f.causes,(f.at+(k==='j'?1:-1)+f.causes.length)%f.causes.length);}
  else if(e.key==='Escape'&&document.querySelector('.cause-bar')){causeFocus=null;const m=document.getElementById('main');if(m)drawCauseFocus(m);}});
const stageName=(x:string)=>x==='submitted'?statusLabel('submitted'):x.charAt(0).toUpperCase()+x.slice(1);
/** A rota in the Drafts picker (the schedule's switcher and the Fix list's): grouped by what you can do with it (SR-01). */
const rotaChoice=(r:Row,more:string[]=[]):Choice=>({value:String(r.id),label:String(r.name),group:draftGroup(r,me!.identity_id),glyph:draftGlyph(r,me!.identity_id),
  hint:[`${formatDate(r.starts_on,zone())} – ${formatDate(lastIncludedDate(r.ends_on),zone())}`,r.status==='draft'?draftHint(r,me!.identity_id):stageName(r.status),...more].filter(Boolean).join(' · ')});
/** Typed words find rotas among every live one (the server's search, before it pages), not only a page already loaded. */
const rotaLookup=(q:string)=>get<{workspaces:Row[];has_more:boolean}>(`/api/workspaces?${new URLSearchParams({limit:'50',offset:'0',q})}`).then(r=>({more:r.has_more,
  choices:r.workspaces.filter(x=>LIVE.includes(x.status)&&!x.archived_at).map(x=>rotaChoice(x))}));
/** A rota's stage, with the whole way to staff in its tip. */
function rotaStatusPill(w:Row,outdated=false){
  const stages=['draft','submitted','approved','published'];
  // L11: one status per rota, from the one palette. A review the server holds out of date (stale_inputs) is "Needs
  // resubmitting", never also "Waiting for approval".
  const state=w.archived_at?'archived':outdated?'outdated':w.status,pill=statusBadge(state,(w.base_publication_id?'Amendment · ':'')+(state==='outdated'?statusLabel(state):stageName(state)));
  pill.classList.add('rota-status');pill.prepend(el('span',{class:'dot','aria-hidden':'true'}));
  if(w.archived_at)return pill;
  pill.dataset.tip=stages.map(x=>(x===w.status?'● ':'')+stageName(x)).join(' → ')+(w.status==='draft'?' · not shared with staff until published':'')
    +(outdated?' · rules, people or availability changed since: the coordinator reopens it and submits it again':'');
  return pill;
}
/** The Fix list's top bar (A1): the rota switcher (another rota's Fix list, or this rota's schedule), its stage, and the
 *  rota's next steps for its coordinator: Auto-schedule (the schedule with its panel open) and Submit for review…. */
function fixBar(data:WorkspaceData,openAuto?:(button:HTMLButtonElement)=>void){
  const w=data.workspace,steps=permits(CAPABILITY.COORDINATOR)&&w.status==='draft'&&!w.archived_at&&!phoneQuery.matches;
  const toRota=()=>{workspaceId=w.id;draftNone=false;navigate(routeHash('schedule',[schedView,planDay],{edit:w.id}).slice(1));};
  const back=el('button',{type:'button',class:'rota-switcher',...popup('dialog'),'aria-label':'Switch draft · '+w.name},el('h1',{},w.name),icon('chevronDown'));
  back.addEventListener('click',async()=>{if(back.getAttribute('aria-expanded')==='true'||back.getAttribute('aria-busy'))return;
    back.setAttribute('aria-busy','true');let live:Choice[];
    try{live=(await rotaLookup('')).choices;}catch(e){reportError(e);return;}finally{back.removeAttribute('aria-busy');}
    if(!back.isConnected)return;
    let close=()=>{};
    const foot=el('div',{class:'popover-foot'},menuItem('Open its schedule','calendar',()=>{close();toRota();},{role:'button'}));
    close=openPicker(back,back.parentElement||document.body,{label:'Drafts',multiple:false,recentKey:'drafts',selected:new Set([w.id]),extra:foot,
      choices:live.some(c=>c.value===w.id)?live:[rotaChoice(w),...live],lookup:rotaLookup,onPick:id=>navigate(routeHash('schedule',['issues'],{draft:id}).slice(1))});});
  // ME-15: on the Fix list the panel opens beside it, in place; elsewhere the schedule opens with its panel.
  const auto:HTMLButtonElement|null=steps?compact(button('Auto-schedule',()=>{const s={...(schedulingByRota.get(w.id)||{...freshScheduling(w.id),...ownSetup(w.id)}),open:true};
    schedulingByRota.set(w.id,s);schedulingSettings=s;if(openAuto)openAuto(auto!);else toRota();},'secondary auto-button',{'aria-controls':'auto-panel','aria-expanded':'false'}),'sparkle'):null;
  const submit=steps?compact(button('Submit for review…',()=>void publishDialog(data).catch(reportError),'secondary',{'data-busy':'Checking…'}),'send'):null;
  const planning=planningOf(data);
  const bar=el('div',{class:'planner-bar review-bar fix-bar'},back,sandboxPill(w,me!.identity_id)||rotaStatusPill(w),planning?groupScopeButton(w.id,planning):'',el('span',{class:'grow'}),auto||'',submit||'');
  fitRow(bar,['tight','tighter','tightest'],()=>phoneQuery.matches||bar.scrollWidth>bar.clientWidth+1);
  return bar;
}
/** Schedule › Fix list (fix_list.ts): its causes act through fixAction; All checks is the per-check table. The rota's
 *  bar on top and its health in the footer (the Fix list's own counts), as on its schedule (A1). */
async function fixPage(target:HTMLElement,id:string){
  if(!id){target.append(note('Open a draft rota first: the Fix list belongs to one draft.','warning'));return;}
  const mySerial=renderSerial;
  // Read on every draw (a conditional read: 304 while nothing changed), not the copy held: a complete check that lands
  // after an edit changes no revision, only the check's evaluated_at and scope, which the Fix list is keyed on
  // (fixListFor). Kept, a held copy left the page on the edit's "Checking…" counts. A newer patched copy still wins.
  const [,data]=await Promise.all([ensureCatalogues(),wholeRota(id,true,false).catch(()=>scheduleCache.drafts.get(id)||null)]);
  const day=String(data?.workspace.starts_on||departmentToday());
  // A saved view is a scheduling group (R2): people by rank, programme or tag, as the person is on the rota's first day.
  const facts=(kind:string,key:string)=>{const by=new Map<string,string[]>();for(const m of rows(kind))if(activeOn(m,day))by.set(m.person_id,[...(by.get(m.person_id)||[]),String(m[key])]);return by;};
  const ranks=facts('memberships','category_id'),programmes=facts('programme_memberships','programme_id'),tags=new Map(rows('people').map(p=>[p.id as string,(p.tags||[]) as string[]]));
  const tagChoices=[...new Set([...tags.values()].flat())].sort().map(t=>({value:t,label:t}));
  const heldBy=(by:Map<string,string[]>)=>new Set([...by.values()].flat());
  // ME-15: the page and the Auto-schedule panel side by side, as on the schedule.
  const column=el('div',{class:'planner-main'}),side=el('aside',{class:'auto-panel',id:'auto-panel','aria-label':'Auto-schedule'});side.hidden=true;
  let opener:HTMLButtonElement|null=null;
  const closeAuto=()=>{side.hidden=true;schedulingSettings.open=schedulingSettings.editing=false;opener?.setAttribute('aria-expanded','false');opener?.focus();};
  sidePanel(side,{key:'auto-schedule',width:420,close:closeAuto,dismiss:()=>{side.hidden=true;schedulingSettings.open=schedulingSettings.editing=false;opener?.setAttribute('aria-expanded','false');}});
  const openAuto=(b:HTMLButtonElement)=>{opener=b;if(!side.hidden){closeAuto();return;}
    if(!data)return;side.hidden=false;b.setAttribute('aria-expanded','true');side.replaceChildren();void schedulingPanel(side,data,true,false,closeAuto).catch(reportError);};
  target.append(el('div',{class:'planner-body'},column,side));
  // FIXLIST-UX: the same fix in place as the Planner's panel (on a phone too, the D10 exception for this flow): chips
  // choose the session, Assign fills it; the page draws its rows again.
  if(fixFocus?.rota!==id)focusOn(id,{});
  const inPlace=():Partial<FixHooks>=>{const l=fixCache.get(id)?.list||lastFixList(id);return l?fixFocusHooks(id,data,l,()=>pageHooks.redraw?.(),false):{};};
  const pageHooks:FixHooks&{redraw?:()=>void}={...fixHooks(id,data),...(permits(CAPABILITY.COORDINATOR)?{chips:c=>inPlace().chips?.(c)||null,fill:c=>inPlace().fill?.(c)||null,assign:c=>inPlace().assign?.(c)||null}:{act:undefined})};
  // One hooks object: the page sets its redraw on it (fix_list.ts), which the chips call.
  const list=await fixListPage(column,id,Object.assign(pageHooks,{read:()=>fixListFor(id,data),step:readyStep(data?.workspace,data),shareable:permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN),planning:(p=>{if(!p)return null;const g=scopedGroup(id,p);return {...p,group_id:g?.group_id??null,group_version_id:g?.group_version_id??(g?p.group_version_id:null)};})(planningOf(data)),
    checks:()=>navigate(routeHash('schedule',['issues'],{draft:id,view:'checks'}).slice(1)),
    personFacts:(p:string)=>({category_ids:ranks.get(p)||[],programme_ids:programmes.get(p)||[],tags:tags.get(p)||[]}),
    // Only what someone on the rota holds that day (F-09: no retired or test programmes, no rank nobody has).
    fields:[{key:'category_ids',label:'Rank',choices:choices(rows('categories').filter(r=>heldBy(ranks).has(r.id)))},{key:'programme_ids',label:'Programme',choices:choices(rows('programmes').filter(r=>heldBy(programmes).has(r.id)))},{key:'tags',label:'Tag',choices:tagChoices}]}));
  if(mySerial!==renderSerial)return;
  if(data)swapPlannerBar(fixBar(data,openAuto),target);
  target.append(footerStatus(fixHealth(id,list.summary,data)));
}
/** What Fix list rows read and do wherever they are drawn: a cause's action, its rule lines and the names in its evidence. */
/** A cause's other fixes, from its own findings' fix hints (leave, a series to extend, the limit, rota rules…): under ▾,
 *  beside the row's one action. Coordinators of the draft only. */
function otherFixes(c:FixCause,data:WorkspaceData|null):HTMLElement[]{
  if(!data||data.workspace.status!=='draft'||!permits(CAPABILITY.COORDINATOR))return [];
  const keys=new Set(c.finding_keys),hints=((data.workspace.validation?.findings||[]) as Row[]).filter(f=>keys.has(issueKey(f))).flatMap(f=>(f.fix_hints||[]) as FindingFixHint[]);
  // Who the check recorded for the cause's first session (planning_context.ts), read from the rota summary on request.
  const people=c.shift_ids.length?button('People for this duty',()=>void loadCauses(data).then(()=>{const o=dutyObservation(data,c.finding_keys);
    if(o)openDutyPeople(data,o);else notify('The check recorded nobody for this duty.','info');}).catch(reportError),'secondary small'):null;
  const leaveDuty=c.action?.scope?.classification==='duties_during_approved_leave';
  const leaveActions=leaveDuty?[button('Replace…',()=>openIssues(data.workspace.id),'secondary small'),
    button('Remove from the duty',()=>void command('issues.resolve',{
      workspace_id:data.workspace.id,action:'unassign',keys:c.finding_keys,
      reason:'Fix list: remove from the duty'},data.workspace.id,data.workspace.revision).then(()=>refresh()).catch(reportError),'secondary small')]:[];
  return [...leaveActions,...fixActions(data,hints,()=>{},[],{except:c.action?.kind,quiet:true}),...(people?[people]:[])];
}
// A cause with sessions is shown in focus (R3); one about people only (a quota) marks them in the People view.
const fixHooks=(id:string,data:WorkspaceData|null,w:Row|null|undefined=data?.workspace):FixHooks=>{let starts:Map<string,string>|undefined;return {act:c=>fixAction(c,id,data),fixes:c=>otherFixes(c,data),
  addSession:w?.status==='draft'&&permits(CAPABILITY.COORDINATOR)?()=>(data?Promise.resolve(data):wholeRota(id)).then(d=>shiftForm(ctx(),d)).catch(reportError):undefined,
  show:(c,all)=>{if(causeShifts(c).length)focusCause(id,all,all.indexOf(c));else if(data){workspaceId=id;draftNone=false;showAffected(data,c.evidence as Row[]);}},
  shows:c=>causeShifts(c).length>0||!!data&&c.person_ids.length>0,
  openLine:l=>void openRuleSource(l.rule_id).catch(reportError),ruleName:rid=>String(find('policies',rid)?.name||''),
  label:(kind,x)=>{if(kind!=='shifts')return name(kind,x);const s=data?.shifts.find(y=>y.id===x);return s?`${s.type_snapshot?.name||'Session'} · ${dayName(s.start)} ${hours(s.start,s.end)}`:x;},
  start:x=>{starts??=new Map((data?.shifts||[]).map(s=>[s.id,String(s.start)]));return starts.get(x);}};};
/** One cause's one action (maintenance action kinds), on its own rota. Nothing here changes the rota: each opens the step that
 *  does (auto-schedule with its scope filled in, the clash in the latest result, the rule line, the session's cover). */
function fixAction(c:FixCause,id:string,data:WorkspaceData|null){
  const a=c.action;if(!a)return;const sc=a.scope||{},day=sc.starts_on||'';
  // Assign (the server's best fix, FIXLIST-BACKEND §3) is done in place: the Fix list at this cause and its session.
  if(ASSIGN_KINDS.has(a.kind)){if(a.available===false){notify(a.unavailable_reason||'No one can be assigned here now.','info');return;}
    openIssues(id,{cause:c.id,shift:String((sc as {shift_id?:string}).shift_id||'')||undefined,chosen:true});return;}
  const open=()=>{workspaceId=id;draftNone=false;if(day)planDay=day;navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));};
  // D16: allowed for this rota with a justification, on the cause's exact binding line, people and dates.
  if(a.kind==='adjust_rota'){const l=c.rule_lines[0],w=data?.workspace||scheduleCache.drafts.get(id)?.workspace,one=sc.person_ids?.length===1?sc.person_ids[0]:'';
    // One person: the rule line panel prefilled from the binding line (B4: Apply to this rota, its effect, or propose).
    if(l?.line_id&&w&&one){void relaxForPerson(referenceContext(),{rule_id:l.rule_id,person_id:one,person_name:name('people',one),binding_line_id:l.line_id,
      number:l.slot!=null?String(l.slot):undefined,week_from:sc.starts_on||undefined,week_to:sc.ends_on||undefined,workspace_id:w.id,workspace_name:w.name,workspace_revision:w.revision}).catch(reportError);return;}
    if(l&&w)adjustForRotaDialog({rule_id:l.rule_id,line_id:l.line_id,slot:l.slot,person_ids:sc.person_ids,starts_on:sc.starts_on,ends_on:sc.ends_on},w,
      {ruleName:rid=>String(find('policies',rid)?.name||''),person:pid=>name('people',pid),done:refresh});
    else if(l)void openRuleSource(l.rule_id).catch(reportError);return;}
  // The server's exact fix hints (maintenance scope.prefills): the sessions the rule needs, or who may work them. Several
  // hints are alternatives, offered side by side (fixActions), never merged into one wider scope.
  if(a.kind==='add_sessions'||a.kind==='open_eligibility'){
    // A session pattern does not depend on whose check asked for it: the same pattern for two people is one choice.
    const seen=new Map((sc.prefills||[]).map(p=>[a.kind==='add_sessions'?JSON.stringify({...p,person_id:null}):JSON.stringify(p),p]));
    const hints=[...seen.values()].map(prefill=>({action:a.kind,prefill})) as FindingFixHint[];
    void (data?Promise.resolve(data):rotaWhole(id)).then(d=>{
      if(hints.length===1&&a.kind==='add_sessions')return newSeries(d,hints[0].prefill);
      const person=hints.length===1?hints[0].prefill.person_id:sc.person_ids?.length===1?sc.person_ids[0]:'',row=person?find('people',person):undefined;
      if(a.kind==='open_eligibility'&&(row||!hints.length)){if(row)personDetails(row);else navigate('people');return;}
      showDialog(actionLabel(c),c.sentence,(body,close)=>body.append(el('div',{class:'actions'},...fixActions(d,hints,close))));}).catch(reportError);return;}
  // TYPE_VERSION_CHANGED (SCHEDULING bcf0c3a64): the sessions take their duty type's current settings, through the same
  // reviewed bulk step as the Schedule's "Refresh type versions" (its impact, then the armed confirm, then Undo).
  if((a.kind as string)==='refresh_sessions'){const ids=causeShifts(c);if(!ids.length)return;
    void (data?Promise.resolve(data):rotaWhole(id)).then(d=>bulkImpactDialog(ctx(),d,ids,'refresh_types',()=>{})).catch(reportError);return;}
  if(a.kind==='open_line'||a.kind==='review_targets'){const l=c.rule_lines[0];if(l)void openRuleSource(l.rule_id).catch(reportError);else navigate('rules');return;}
  if(a.kind==='choose_cover'){const s=data?.shifts.find(x=>sc.shift_ids?.includes(x.id));
    if(s){workspaceId=id;draftNone=false;queueFill(id,s.id);toCalendar(s);navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));}else open();return;}
  // A scoped run (Plan their weeks, Rebalance): Auto-schedule opens on What to fill with these people and dates, keeping
  // their existing duties unless the server says otherwise; Run stays the coordinator's press. The clash: its Review.
  const s={...(schedulingByRota.get(id)||{...freshScheduling(id),...ownSetup(id)}),open:true,step:(a.kind==='clash'?3:1) as 0|1|2|3};
  if(a.kind!=='clash'){Object.assign(s,{scope:'period',from:sc.starts_on||s.from,to:sc.ends_on||s.to,keep:sc.keep_existing??true,
    group:sc.person_ids?.length?{match:'all',conditions:[{field:'person_ids',op:'any',values:sc.person_ids}]}:null,groupKeep:sc.keep_existing??true,pickGroup:false});keptSetup(s);}
  schedulingByRota.set(id,s);schedulingSettings=s;open();
}
const issueHooks=():IssuesHooks=>({name,find,rows,zone,source:referencePolicy,loadSources:async ids=>{if(ids.some(id=>!find('policies',id)?.definition))await loadRetired();},openRule:(id,person)=>{void openRuleSource(id,person).catch(e=>reportError(e));},editable:w=>w.status==='draft'&&!w.archived_at&&permits(CAPABILITY.COORDINATOR),
  // Leaves the Issues page for this issue's draft on the session's day, marking the session once drawn (?shift=).
  showOnSchedule:(id,s)=>{workspaceId=id;draftNone=false;toCalendar(s);navigate(routeHash('schedule',[schedView,planDay],{edit:id,shift:String(s.id)}).slice(1));},
  fill:(id,s,issue)=>openFill(id,s,issue),
  back:id=>{workspaceId=id;draftNone=false;navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));},fixList:openIssues,
  exceptions:(items,done)=>exceptionForm(referenceContext(),{items},done),causes:data=>loadCauses(data),context:referenceContext,
  // All checks' cause groups: the same cause cards, fixes and People for this duty (causeGroupIssue), only for this check
  // (heldCauses is keyed by revision and fingerprint). A fix acts on this draft, as the Schedule's cards do.
  causeCards:(data,keys,close)=>{const want=new Set(keys),byKey=new Map(((data.workspace.validation?.findings||[]) as Row[]).map(f=>[issueKey(f),f]));
    const done=()=>{workspaceId=data.workspace.id;draftNone=false;close();};
    return issueList((heldCauses(data)||[]).filter(g=>g.finding_keys.some(k=>want.has(k))).flatMap(g=>{const found=g.finding_keys.map(k=>byKey.get(k)).filter(f=>f?.cause) as Row[];
      return found.length?[causeGroupIssue(data,g,found,done)]:[];}),{label:'Cause and fixes',done});},
  // The rota's job list is the Schedule's (read with its sessions); Resolve issues shows the proposal it holds.
  // Its draft read is the one the draft-edit guard (guardDraftEdits) asks about, as the Schedule's is.
  proposal:data=>{scheduleCache.drafts.set(data.workspace.id,data);return readJobs(data).then(()=>proposalNote(data));}});
// Run results prefix older messages with the person and session, which the card already names.
const runIssueWords=(f:Row)=>{const m=String(f.message),i=m.indexOf(': ');return f.protected&&!f.technical&&(f.shift_id||f.person_id)&&i>=0?m.slice(i+2):m;};
function runIssueCard(data:WorkspaceData,f:Row,s:Row|undefined,done:()=>void,interactive=true):Issue{
  const finding={...f,severity:f.severity||'error',message:runIssueWords(f)};
  return findingCard(finding,s,interactive?findingActions(data,finding,s,done):[]);
}
/** Problems an auto-schedule run reported (ones it must keep and cannot start with, or ones outside its sessions), said
 *  as issue cards: one per person and session, grouped by kind with counts, the first few of each kind shown. */
function runIssues(data:WorkspaceData,found:Row[],label:string,done:()=>void=()=>{}):HTMLElement{
  const byId=new Map(data.shifts.map(s=>[s.id,s])),FIRST=5;
  const cards=new Map<string,Row[]>();
  for(const f of found){const k=f.shift_id||f.person_id?`${f.shift_id}|${f.person_id}`:`${f.code}|${f.start}|${f.message}`;cards.set(k,[...(cards.get(k)||[]),f]);}
  const kinds=new Map<string,Issue[]>(),byKey=new Map(found.map(f=>[issueKey(f),f])),grouped=new Set<string>();
  // Problems the draft's check explains by one cause are one card with the cause and its fix (as in the footer lists).
  for(const g of heldCauses(data)||[]){const hit=g.finding_keys.map(k=>byKey.get(k)).filter(f=>f?.cause) as Row[];if(!hit.length)continue;
    for(const f of hit)grouped.add(`${f.shift_id}|${f.person_id}`);
    const kind=issueKind(hit[0],hit[0].rule_id?find('policies',hit[0].rule_id)?.name:undefined);kinds.set(kind,[...(kinds.get(kind)||[]),causeGroupIssue(data,g,hit,done)]);}
  const total=cards.size;
  for(const k of grouped)cards.delete(k);
  for(const list of cards.values()){
    const f=list[0],s=f.shift_id?byId.get(f.shift_id):undefined;
    const issue=runIssueCard(data,f,s,done),also=[...new Set(list.slice(1).map(runIssueWords))].filter(m=>m!==runIssueWords(f));
    if(also.length)issue.detail=[issue.detail,'Also: '+also.join('; ')].filter(Boolean).join(' · ');
    const kind=issueKind({...f,message:runIssueWords(f)},f.rule_id?find('policies',f.rule_id)?.name:undefined);
    kinds.set(kind,[...(kinds.get(kind)||[]),issue]);
  }
  const groups=[...kinds].sort((a,b)=>b[1].length-a[1].length);
  return el('section',{class:'run-issues','aria-label':label},
    el('div',{class:'needs-head'},el('h3',{},label),el('span',{class:'wk-count'},String(total)),el('span',{class:'grow'}),
      data.workspace.status==='draft'?button('Open Fix list',()=>{done();openIssues(data.workspace.id);},'secondary small'):''),
    ...groups.map(([kind,items])=>{
      return el('div',{class:'run-kind'},groups.length>1?el('h4',{},el('span',{class:'grow'},kind),el('span',{class:'wk-count'},String(items.length))):'',
        issueList(items,{label:`${kind} (${items.length})`,limit:FIRST,expand:true,done}));}),
    // The footer's blocking list holds the draft's own check (the same problems once the draft is checked).
    total>FIRST&&data.workspace.validation?.error_count?button('Open all in the footer list',()=>{done();document.querySelector<HTMLButtonElement>('.app-footer .health-pill.err')?.click();},'link-button'):'');
}
/** "Allow an exception…": the ordinary exception request (approved separately), filled in with the rule, person and day. */
function exceptionButton(ruleId:string,personId:string,iso:string,label='Allow an exception…'){
  if(!permits(CAPABILITY.COORDINATOR))return null;
  const rule=referencePolicy(ruleId),make=()=>button(label,()=>exceptionForm(referenceContext(),{policy_id:ruleId,person_id:personId,day:toLocal(iso,zone()).slice(0,10)}),'ghost small');
  if(rule?.definition)return rule.status==='active'&&allowsException(rule)?make():null;
  const slot=make();slot.disabled=true;
  void loadRetired().then(()=>{const effective=referencePolicy(ruleId);if(effective?.status==='active'&&allowsException(effective))slot.disabled=false;else slot.remove();}).catch(()=>slot.remove());
  return slot;
}
/** A rule-check finding as a card of the one issue item (issue_marker.ts findingIssue: its severity, session and Fix list
 *  key), with whose it is, the session, the rule and its actions (UIUX-AUDIT C1: no second findingIssue). */
function findingCard(f:Row,s:Row|undefined,actions:Issue['actions']):Issue{
  const rule=f.rule_id?find('policies',f.rule_id)?.name:undefined;
  // Leave: "Dr A: On leave or away · 5–9 Oct" with its type as a pill and its note (as far as the department shows them).
  const leave=f.code==='UNAVAILABLE'?f.leave:undefined;
  return {...findingItem(f),words:(f.person_id?name('people',f.person_id)+': ':'')+(leave?`${leaveLabel()} · ${leaveDates(leave)}`:f.message),pill:leave,
    session:s?[s.type_snapshot.name,(s.service_ids as string[]).map(x=>name('services',x)).join(' + '),formatDay(s.start,zone(),true),`${formatTime(s.start,zone())}–${formatTime(s.end,zone())}`].filter(Boolean).join(' · '):f.start?formatDate(f.start,zone()):undefined,
    // A rule's own finding is usually worded as the rule's name: then the title already says it.
    rule:rule&&rule!==f.message?{name:rule,mandatory:f.severity==='error'}:undefined,reason:causeText(f)||undefined,cause:f.cause?.code,
    detail:leave?[leave.note,alsoShort(f)].filter(Boolean).join(' · ')||undefined:[findingNumbers(f),rule&&rule===f.message?(f.severity==='error'?'Mandatory rule':'Advisory rule'):''].filter(Boolean).join(' · ')||undefined,actions};
}
/** Brings a session into view on the schedule and marks it for a moment. */
function showOnSchedule(s:Row){
  toCalendar(s);if(view!=='schedule')navigate('schedule');
  void renderPage().then(()=>requestAnimationFrame(()=>markShift(String(s.id))));
}
/** Scrolls to a session and rings it; one the view does not draw (the search, filters or layers hide it) is said. */
function markShift(id:string,person=''){
  const spots=[...document.querySelectorAll<HTMLElement>(`[data-rk*='"shift":"${CSS.escape(id)}"']`)];
  if(!spots.length){notify('That session is not shown with the current view, search or filters.','info');return;}
  // A link naming a person on the session (G-05) focuses their chip, not the session's first.
  const first=spots.find(n=>person&&n.dataset.person===person)||spots[0];
  first.scrollIntoView({block:'center',inline:'nearest'});first.focus({preventScroll:true});
  for(const n of spots){n.classList.remove('issue-spot');void n.offsetWidth;n.classList.add('issue-spot');}
}
/** The current absence warnings on the published sessions in view, one per duty (publication, session and person). */
const absentOnDuty=(layer:ScheduleWindow,shown:Pick<WorkspaceData,'shifts'>):PublishedOperationalWarning[]=>{const ids=new Set(shown.shifts.map(s=>s.id));
  return [...new Map(layer.sources.flatMap(s=>((s.operational as PublishedOperationalSummary|undefined)?.warnings||[])).filter(x=>ids.has(x.shift_id)).map(x=>[x.key,x])).values()];};
/** One duty during approved absence: who and when, the session, and (coordinators) the reviewed amendment that changes it. */
function absenceIssue(a:PublishedOperationalWarning,s:Row|undefined,close:()=>void,person?:string,replace=true):Issue{
  // A coordinator replaces the person in the session's own panel (B1), not through a whole-month amendment.
  // `replace` false: the plain warning this one builds on (calling it with the same session again never ended).
  // An amendment already open is continued instead, as Home's row does.
  if(replace&&s&&!a.action?.amendment_workspace_id&&permits(CAPABILITY.COORDINATOR)&&changeable(s)&&s.participants.some((p:Row)=>p.id===a.assignment_id))
    return {...absenceIssue({...a,action:null},s,close,person,false),actions:[button('Replace…',()=>{close();replaceNext(String(s.id),a.assignment_id);readOnlySession(s);},'secondary small')]};
  const act=a.action,amend=act?button(act.amendment_workspace_id?'Open amendment':'Change published rota…',async()=>{close();
    if(act.amendment_workspace_id){openAmendment(act,navigate);return;}
    const p=(await publishedInView()).find(x=>x.id===act.publication_id);if(p)workspaceForm(ctx(),{...p});else notify('This publication is no longer in force. Refresh the schedule.','warning');},'secondary small'):null;
  return {severity:'error',shift:a.shift_id,words:person||name('people',a.person_id),session:s?`${s.type_snapshot?.name||'Session'} · ${dayName(a.start)} ${hours(a.start,a.end)}`:`${dayName(a.start)} ${hours(a.start,a.end)}`,
    detail:'Recorded unavailability overlaps this published duty; it stays on the published rota until it is changed.',
    actions:[s?button('Details',()=>{close();readOnlySession(s);},'ghost small'):null,amend]};
}
/** The same warning for team readers and staff; names come only from the authorized window. */
function absencePill(absent:PublishedOperationalWarning[],shifts:Row[],people?:Record<string,Row>){
  const byId=new Map(shifts.map(s=>[s.id,s]));
  // SW3-C-01: a staff reader's own duty is information (cover is the coordinator's to arrange, D13), not an error to fix.
  const own=!permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR);
  return healthPill(null,own?'info':'err',own?'leave':'alert',`${absent.length} unavailable on duty`,publishedAbsenceLabel(absent.length,own),
    close=>absent.map(a=>absenceIssue(a,byId.get(a.shift_id),close,people?.[a.person_id]?.display_name)));
}
/** The rota's summary with its session records (GET /summary), read when a list of its open places opens. */
const rotaSummary=(id:string)=>get<RotaCauseSummary>(`/api/workspaces/${encodeURIComponent(id)}/summary?view=causes`);
/** A health count and its on-demand issue list, shared by draft, published team and own-duty warnings. */
function healthPill(data:WorkspaceData|null,tone:string,name:IconName,text:string,label:string,items:(close:()=>void)=>Issue[],load?:()=>Promise<unknown>,grouped?:(close:()=>void)=>HTMLElement,sub=''){
  const [count,...words]=text.split(' ');
  const key=(data?.workspace.id||'published')+':'+tone,moved=pillCounts.has(key)&&pillCounts.get(key)!==count;pillCounts.set(key,count);
  const b=el('button',{type:'button',class:`health-pill ${tone}`,'aria-haspopup':'dialog','aria-expanded':'false','aria-label':label,'data-ctip':label},icon(name),el('span',{class:'num'+(moved?' num-moved':'')},count),el('span',{class:'label'},' '+words.join(' ')));
  footerAct(b,'health:'+key,async()=>{if(load&&b.getAttribute('aria-expanded')!=='true')await load().catch(reportError);popover(b,(panel,close)=>{
    const list=grouped?[]:items(close);panel.classList.add('issue-pop');
    panel.append(el('div',{class:'popover-head'},el('strong',{},label),''),sub?el('p',{class:'popover-section muted repair-units'},sub):'',grouped?grouped(close):list.length?issueList(list,{label,limit:60,done:close}):emptyLine('Nothing to show.',null,'popover-section'),list.length>60?el('p',{class:'popover-section muted'},`Showing 60 of ${list.length}. ${tone==='warn'?'Filter the schedule to "Only sessions with open places"':'Filter the schedule to "Only sessions that break a rule"'} to see every one.`):'');
  },{label,placement:'below',key:'health:'+key});});
  // A footer redrawn while its list is open (a refresh, a late read) hands the list to the new pill instead of closing it.
  queueMicrotask(()=>adoptPopover('health:'+key,b));
  return b;
}
/** Published health counts come from the server's projection of issued checks, across all publications in view. Its
 *  Must fix and Could improve are the one rota health (fix_list.ts fixPills, UIUX-AUDIT C3): the same pills, shapes,
 *  tones and words as a draft's, each opening the issued check's list; the published layer's own signals (absences on
 *  duty, extra cover, the score) sit beside them. */
function healthPills(data:WorkspaceData,fresh:boolean,counts:NonNullable<ScheduleWindow['summary']>,absent:PublishedOperationalWarning[]=[]){
  const v=data.workspace.validation;
  let wrap:HTMLElement=el('div',{class:'health-pills',role:'group','aria-label':'Rota health'});
  if(proposalShown(data))wrap.append(proposalPill(data));
  // The current absence warning is the server's, apart from any stored check: it shows with or without one.
  if(!v){wrap.append(el('span',{class:'health-pill neutral'},icon('clock'),'Not checked yet'));if(absent.length)wrap.append(absencePill(absent,data.shifts));return wrap;}
  const byId=new Map(data.shifts.map(s=>[s.id,s]));
  const {sessions_with_gaps:gaps,blocking:errors,advisory:warnings,extra_cover_sessions:extra}=counts;
  // The lists group findings by cause: the check's cause groups are read when a list first opens (once per check),
  // not on every draw, so an edit costs only its command and the draft read.
  const causes=errors||warnings?()=>loadCauses(data):undefined;
  // Sessions short of people are must-fix causes too (the server counts them in `must`): one pill lists both, the open
  // places first, never a separate "need staff" count beside it.
  const must=counts.must||(errors?errors:0)||(gaps?gaps:0),could=counts.could||warnings;
  if(must||could){const shown=wrap;wrap=fixPills({must,could,checking:false,submission_issue:null,score:null,checked_at:null} as unknown as FixSummary,{step:null,clear:fresh?'All clear':'Was clear',
    lists:{must:close=>{const r=rotaIssues(data,close),xs=[...r.gaps,...r.otherErrors];
        return xs.length?issueList(xs,{label:mustWords(must),limit:60,done:close}):emptyLine('Nothing to show.',null,'popover-section');},
      could:close=>groupedWarnings(data,close)},before:causes});
    wrap.prepend(...shown.childNodes);}
  // Published days: an approved absence that now overlaps an issued duty (the server's current warning, apart from the
  // stored check the other pills read). The publication stays as approved; the duty still needs cover.
  if(absent.length)wrap.append(absencePill(absent,data.shifts));
  if(extra)wrap.append(healthPill(data,'info extra-cover','clock',`${extra} extra cover open`,EXTRA_COVER_OPEN(extra),close=>rotaIssues(data,close).extraCover));
  if(!data.shifts.some(s=>!s.cancelled)&&!must&&!could&&!extra)wrap.append(el('span',{class:'health-pill neutral','data-ctip':'No sessions yet'},icon('calendar'),el('span',{class:'label'},'No sessions yet')));
  else if(!must&&!could&&!extra&&!absent.length){const t=fresh?'All clear':'Was clear';wrap.append(el('span',{class:'health-pill ok','data-ctip':t},icon('check'),el('span',{class:'label','aria-hidden':'true'},t),el('span',{class:'sr-only'},t)));} // a tight bar (phones) shows just the tick
  // Keyed by the rota and its session count: adding or removing sessions (an empty rota filled from templates) starts a
  // new baseline, so the footer never claims a jump like −100 from an empty rota; edits to assignments show their change.
  // No score while there is nothing to score (an empty rota): "No sessions yet" says it, no second "n/a" pill (G-41).
  if(v.score&&shownScore(v.score)!=null)wrap.append(scorePill(data.workspace.id+':'+data.shifts.length,v.score,id=>name('people',id),id=>{const s=byId.get(id);if(s&&!openSession(s))readOnlySession(s);},
    data.workspace.status==='draft'?reviewScores(data.workspace.id):undefined));
  return wrap;
}

/** A finding's kind by its code alone ("Time overlap"; leave in the department's words): rule-less lists and run prechecks. */
const findingCodeLabel=(f:Row)=>f.code==='UNAVAILABLE'?issueKind(f):plainCode(f.code)||String(f.code||'Warning').toLowerCase().replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
// Run prechecks (job.progress.precheck_issues) by classification, blocking first, as the contract names them.
const PRECHECK_GROUPS:[string,string,Tone][]=[['blocking',LEVEL_WORDS.must,'err'],['protected','Kept as they are','warn'],['unresolved','Not resolved yet','info'],['outside_scope','Outside these sessions','info']];
/** What a run's checks found before its search: one line per code with its count and up to three sample sessions/rules. */
function precheckIssues(data:WorkspaceData,issues:Row[]):HTMLElement{
  const byId=new Map(data.shifts.map(s=>[s.id,s])),total=issues.reduce((n,i)=>n+(Number(i.count)||0),0);
  const sample=(x:Row)=>{const s=x.shift_id?byId.get(x.shift_id):undefined,who=x.person_id?name('people',x.person_id):'',rule=x.rule_id?find('policies',x.rule_id):undefined;
    return [s?button(`${formatDate(s.start,zone())} · ${s.type_snapshot?.name||'Session'}${who?` · ${who}`:''}`,()=>showOnSchedule(s),'link-button'):who?el('span',{},who):'',
      rule?button(String(rule.name),()=>{void openRuleSource(rule.id,x.person_id||undefined).catch(e=>reportError(e));},'link-button'):''].filter(Boolean);};
  return el('section',{class:'run-issues','aria-label':'Found so far'},el('div',{class:'needs-head'},el('h3',{},'Found so far'),el('span',{class:'wk-count'},String(total))),
    ...PRECHECK_GROUPS.map(([key,title,tone])=>{const list=issues.filter(i=>i.classification===key);
      // The code in the contract's words with its count, as one item of the one issue list (UIUX-AUDIT C1); its sample
      // sessions and rules are its actions (each goes to its session or opens its rule).
      return list.length?issueList(list.map((i):Issue=>({severity:key==='blocking'?'error':'warning',words:`${findingCodeLabel(i)} · ${Number(i.count)||0}`,detail:title,
        actions:((i.samples||[]) as Row[]).slice(0,3).flatMap(sample).filter((n):n is HTMLElement=>typeof n!=='string')})),{label:title}):'';}));
}
/** Warnings in one shared collection, grouped by rule with each group's count: every warning is a visible row (person,
 *  day, what it says), paged, searchable and regroupable (by person or day), so hundreds stay readable without hiding any. */
/** The published rota's could improve (UIUX live check F3): the one issue list, each finding's one sentence once, its
 *  sessions as chips naming who and when (the chip shows that session), must fix first, as everywhere else. */
function groupedWarnings(data:WorkspaceData,close:()=>void){
  const {warningRows}=rotaIssues(data,close),byId=new Map(data.shifts.map(s=>[s.id,s]));
  const at=(f:Row)=>{const s=f.shift_id?byId.get(f.shift_id):undefined;return s?String(s.start):String(f.start||'');};
  const rows=[...warningRows].sort((x,y)=>at(x).localeCompare(at(y)));
  const days=dayLabels(rows.map(f=>{const t=at(f);return t?{day:formatDay(t,zone()).split(' ').slice(0,2).join(' '),time:formatTime(t,zone())}:{day:'',time:''};}));
  const xs=rows.map((f,n)=>findingItem(f,[f.person_id?name('people',f.person_id):'',days[n]].filter(Boolean).join(' · ')));
  return xs.length?issueList(xs,{label:couldWords(xs.length),limit:60,expand:true,done:close}):emptyLine('Nothing to show.',null,'popover-section');
}
/** Lock or unlock draft sessions in one click: no reason or preview (a draft change with Undo in the message).
 *  Auto-schedule keeps a locked session exactly as it is. */
async function setLocked(data:WorkspaceData,shifts:Row[],on:boolean){
  if(!shifts.length)return;
  // Every Lock and Unlock comes here: held (sessionsHeld), it says why instead of sending what the server refuses.
  if(sessionsHeld(data)){notify(onlyRotaCoordinator('lock'),'info');return;}
  await command('shift.action',{workspace_id:data.workspace.id,shift_ids:shifts.map(s=>s.id),action:on?'lock':'unlock',reason:on?'Locked on the schedule':'Unlocked on the schedule'},undefined,data.workspace.revision);
  const what=shifts.length===1?'Session':plural(shifts.length,'session');
  notify(on?`${what} locked: auto-schedule keeps ${shifts.length===1?'it':'them'} as ${shifts.length===1?'it is':'they are'}.`:`${what} unlocked.`);
  await refresh();
}
/** The signed-in staff member's own absences (recorded or approved leave), read once per minute at most. */
type Span={start:number;end:number};
let leaveRead:{at:number;who:string;rows:Promise<Span[]>}|null=null;
function myLeave():Promise<Span[]>{
  // Kept for one account only: another sign-in in this tab reads its own.
  if(!leaveRead||leaveRead.who!==me!.identity_id||Date.now()-leaveRead.at>60000)leaveRead={at:Date.now(),who:me!.identity_id,rows:get<{availability:Row[]}>('/api/availability').then(r=>r.availability.filter(a=>!a.retracted).map(a=>({start:Date.parse(a.start),end:Date.parse(a.end)})))};
  return leaveRead.rows;
}
/** The Schedule's scroll (the planner column and the view's own scroller, the week or month grid), read as a redraw
 *  swaps the view in (not when it starts: a scroll made while it loads is the one kept); the result puts it back when
 *  the redrawn view is the same one (rota, saved view, layout and layer: its viewKey), so another rota or view starts
 *  at the top. The list's table keeps its own (table_view). */
let stopScrollRestore=()=>{};
// The put-back in force: where it aims, and where it last left each scroller (short of the aim until the grid has its height).
let scrollKept:{view:string,kept:number[][],left:number[][]}|null=null;
// Still where the put-back left it, or as near as its content now allows (a collapsed grid clamps its scroll): nothing else moved it.
const scrollHeld=(n:HTMLElement,p:number[])=>[[n.scrollTop,p[0],n.scrollHeight-n.clientHeight],[n.scrollLeft,p[1],n.scrollWidth-n.clientWidth]]
  .every(([now,was,max])=>Math.abs(now-was)<=1||Math.abs(now)<Math.abs(was)&&Math.abs(now)>=max-1);
function plannerScroll(){
  const at=()=>[...document.querySelectorAll<HTMLElement>('#main .planner-main,#main .planner-main .wk-scroller')];
  const key=()=>document.querySelector<HTMLElement>('#schedule-view')?.dataset.viewKey,view=key(),was=scrollKept?.view===view?scrollKept:null;
  // A scroller the last put-back is still bringing to its place keeps that aim (read now, it would say the clamped place).
  const kept=at().map((n,i)=>was?.left[i]&&scrollHeld(n,was.left[i])?was.kept[i]:[n.scrollTop,n.scrollLeft]),left:number[][]=[];
  stopScrollRestore();  // the previous draw's put-back must not fight this one
  const put=()=>at().forEach((n,i)=>{if(kept[i]){n.scrollTop=kept[i][0];n.scrollLeft=kept[i][1];left[i]=[n.scrollTop,n.scrollLeft];}});
  // The grid's fitting (observeSize: pill names, toolbar rows) changes its height after the draw, however many frames
  // later a loaded machine takes, which clamped the scroll just put back (Month to the top): each size change of what
  // scrolls puts it back again, until the person moves it, another view is shown or the next draw reads it afresh.
  const watch=()=>{for(const n of at())for(const e of ['wheel','pointerdown','keydown','touchstart'])n.addEventListener(e,()=>stopScrollRestore(),{once:true,passive:true});};
  return ()=>{if(!view||key()!==view)return;put();watch();
    // Only while every scroller is where the put-back left it: a scroll made since (the page's own too, which fires none
    // of the events above) is newer than the aim and ends it.
    const stops=at().flatMap(n=>[...n.children]).map(c=>observeSize(c,()=>{if(key()===view&&at().every((n,i)=>!left[i]||scrollHeld(n,left[i])))put();else stopScrollRestore();}));
    scrollKept={view,kept,left};
    stopScrollRestore=()=>{stops.forEach(s=>s());scrollKept=null;stopScrollRestore=()=>{};};};
}
// A published session (the published layer) opens read-only; set by the Schedule for the sessions it draws.
let readOnlySession:(s:Row)=>void=()=>{};
/** An undoable draft edit's optional note (OD-17): confirmAction's routine wording; routineReason fills an empty one. */
const routineNote=()=>{const r=area('Change reason','',2);r.input.maxLength=500;r.input.placeholder='Optional note, recorded in the history';return r;};
function copySelected(data:WorkspaceData){
  const ids=[...selected],first=data.shifts.filter(s=>selected.has(s.id)).map(s=>toLocal(s.start,zone()).slice(0,10)).sort()[0]||toLocal(new Date().toISOString(),zone()).slice(0,10);
  const shift=(d:string,n:number)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10);};
  const days=(a:string,b:string)=>Math.round((Date.parse(b+'T12:00:00Z')-Date.parse(a+'T12:00:00Z'))/864e5);
  // S-12: the first target day (the shared date picker) with presets, not a day-offset number. OD-17: an undoable draft
  // edit takes an optional note; left empty, the history records the routine reason.
  formDialog(`Copy ${plural(ids.length,'session')} to…`,'',form=>{
    const target=field('First day of the copies','date',shift(first,7),{required:true,min:shift(first,-92),max:shift(first,92)});
    const nextMonth=shiftMonth(first,1);  // clamped: 31 Jan → 28/29 Feb, 31 May → 30 Jun
    const presets=el('div',{class:'actions sched-presets'},...([['+1 week',shift(first,7)],['+2 weeks',shift(first,14)],['Next month',nextMonth]] as [string,string][])
      .map(([label,day])=>button(label,()=>{target.input.value=day;target.input.dispatchEvent(new Event('change',{bubbles:true}));},'secondary small')));
    // The server copies at most 100 sessions in one change: say so beside the day, before Save. inlineCheck re-applies it
    // after the date picker's own check, which clears custom validity on every new day (a preset, typing, the calendar).
    inlineCheck(target,()=>ids.length>100?`Copy up to 100 sessions at a time (${ids.length} selected).`:'');
    const reason=routineNote();
    form.append(target.wrap,presets,reason.wrap);
    const save=draftImpact(form,ctx(),()=>({kind:'shift.copy',expected_revision:data.workspace.revision,payload:{workspace_id:data.workspace.id,shift_ids:ids,offset_days:days(first,target.input.value),reason:routineReason(reason.input.value)}}));
    return async()=>{await save();selected.clear();await refresh();};
  },'Copy into draft');
}
function swapDialog(data:WorkspaceData,firstId='',secondId=''){
  // Each assignment once, grouped by day for the shared searchable picker. With the first side given (a drop, or the chip's
  // Exchange with…), the second lists only that week's other sessions (SR-14), not every assignment in the rota.
  const day=(s:Row)=>toLocal(s.start,zone()).slice(0,10),js=((me!.organisation.week_start??6)+1)%7;
  const from=data.shifts.find(s=>s.participants.some(p=>p.id===firstId));
  const weekOf=(d:string)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()-(x.getUTCDay()-js+7)%7);return x.toISOString().slice(0,10);};
  const options=(near:boolean)=>data.shifts.filter(s=>!s.context&&!isClosed(s)&&!s.locked&&(!near||!from||s.id!==from.id&&weekOf(day(s))===weekOf(day(from))))
    .sort((a,b)=>String(a.start).localeCompare(String(b.start))).flatMap(s=>s.participants.map(p=>({value:p.id,label:`${name('people',p.person_id)} · ${formatDay(s.start,zone())} · ${s.type_snapshot.name} ${formatTime(s.start,zone())}`,group:formatDay(s.start,zone())})));
  // OD-17: an optional note. The impact is worked out as soon as both sides are chosen (SI-23): one Save, no second step.
  formDialog('Exchange two assignments','',form=>{
    const a=select('First assignment',options(false),firstId),b=select('Second assignment',options(true),secondId),reason=routineNote();form.append(a.wrap,b.wrap);
    // The picked name's date and time are in its label (H-03): one person can be on several sessions that week.
    const shiftOf=(id:string)=>data.shifts.find(s=>s.participants.some(p=>p.id===id))?.id;
    const save=draftImpact(form,ctx(),()=>({kind:'draft.swap',expected_revision:data.workspace.revision,payload:{workspace_id:data.workspace.id,first_assignment_id:a.input.value,second_assignment_id:b.input.value,reason:routineReason(reason.input.value)}}),true,
      ()=>[shiftOf(a.input.value),shiftOf(b.input.value)].filter(Boolean) as string[]);
    form.append(reason.wrap);
    return async()=>{await save();notify('Both sides of the exchange were saved together.');await refresh();};
  },'Exchange');
}
/** `chosen`: the recurring session to start from (a breach's fix, or one just created for it). */
function seriesMaterialise(data:WorkspaceData,chosen=''){
  formDialog('Add recurring sessions','Adds their sessions to this draft. If one has changed, use Update recurring sessions instead, so nothing is added twice.',form=>{
    const options=choices(rows('recurring_series').filter(r=>r.active&&r.starts_on<data.workspace.ends_on&&(!r.ends_on||r.ends_on>data.workspace.starts_on)));
    // One field for one or several (the shared multiple-choice picker, with search and Select all).
    const patterns=multiPick('Recurring sessions',options,chosen?[chosen]:options.length===1?[options[0].value]:[],undefined,'Choose recurring sessions');
    form.append(el('div',{class:'pick-field span-2'},el('span',{class:'pick-label'},'Recurring sessions'),patterns.wrap));
    const save=draftImpact(form,ctx(),()=>{
      if(!patterns.values().length)throw new Error('Choose at least one recurring session.');
      return {kind:'series.expand',expected_revision:data.workspace.revision,payload:{workspace_id:data.workspace.id,series_ids:patterns.values()}};
    });
    return async()=>{const result=await save();notify(plural(result.created_count,'session')+' added to the draft.');await refresh();};  // the message takes the Undo
  },'Add sessions');
}
function seriesFollowing(data:WorkspaceData){
  formDialog('Update recurring sessions','Only this draft changes, from the date you choose: matching dates are updated in place and dates no longer in the series are removed. Earlier dates, published rotas and locked sessions stay as they are.',form=>{
    const series=select('Recurring session',choices(rows('recurring_series'))),from=field('From','date',data.workspace.starts_on,{required:true,min:data.workspace.starts_on,max:data.workspace.ends_on});
    const people=select('Assigned people',[{value:'preserve_offsets',label:'Keep the people already assigned'},{value:'clear',label:'Remove the people from the updated sessions'}],'preserve_offsets');
    const replace=check('I reviewed replacing manual changes, closed sessions and assigned people',false),reason=area('Change reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    people.wrap.classList.add('span-2');// How dates and people carry over is explanation, not a warning: contextual help on the From field (S-13).
    from.wrap.querySelector('label')?.after(el('button',{type:'button',class:'icon-button bare field-help','aria-label':'How dates carry over','data-help':'','data-tip':'Dates follow the recurring session’s own time zone. People are never cut short to fit a shorter session.'},icon('help')));
    form.append(series.wrap,from.wrap,people.wrap,replace.wrap,reason.wrap);
    const save=draftImpact(form,ctx(),()=>({kind:'series.reconcile',expected_revision:data.workspace.revision,payload:{workspace_id:data.workspace.id,series_id:series.input.value,expected_series_revision:find('recurring_series',series.input.value)?.revision,from_on:from.input.value,replace_modified:replace.input.checked,participant_policy:people.input.value,reason:reason.input.value}}));
    return async()=>{const result=await save();notify(`${result.updated_count} updated, ${result.created_count} added, ${result.removed_count} removed; ${result.preserved_count} earlier sessions kept. Draft only.`);await refresh();};
  },'Update sessions');
}

/** What a coordinator's Configuration holds (they may change tags; everything else is the admin's). */
// Auto-schedule & scoring: a coordinator's is the Score review only (they propose rule changes); settings stay admin's.
// Record sections (Tags, Announcements…) are not listed here: catalogueSection adds every one this person may edit.
const COORDINATOR_KINDS=['setup','scheduling_groups','auto_schedule'];
// Every grouped kind of CONFIG_KINDS (config_kinds.ts), the one list: a record kind added there gets its section here.
const ALL_CONFIG_KINDS=CONFIG_GROUPS.flatMap(([,kinds])=>kinds).filter(k=>k!=='setup');
/** The admin's sections this person may open: the one list behind the section menu, its addresses and the palette. */
const configKinds=()=>ALL_CONFIG_KINDS.filter(k=>(!SECTION_PERMISSION[k]||may(SECTION_PERMISSION[k]))&&(k!=='ai_provider'||scoreCheckOn()));
/** The sections this person's Configuration holds: an admin's all, a coordinator's guide and tags, and any section its own
 *  permission grants. Admins reach branding from Department settings. */
const configSections=()=>permits(CAPABILITY.ADMIN)?['setup',...configKinds().filter(k=>k!=='branding')]:[...new Set([...(permits(CAPABILITY.COORDINATOR)?COORDINATOR_KINDS:[]),...configKinds().filter(k=>SECTION_PERMISSION[k]||catalogueSection(k))])];
/** A record section this person may edit: its Bulk edit command's roles (editRoles) admit them, so whatever Bulk lets
 *  them change has its own single edit in Configuration too (M1: Bulk-editable == single-edit). */
const catalogueSection=(kind:string)=>!!editRoles(kind)&&catalogueEditable(kind);
/** Only form-backed, named records with an existing permitted page are palette destinations. */
function catalogueSources():[string,string[]][]{
  return [['configuration',allowed('configuration')?configKinds().filter(k=>!isSettings(k)&&catalogueEditable(k)&&configSections().includes(k)):[]],
    ['programmes',allowed('programmes')?['programmes','cohorts','rotation_types','programme_calendar_entries']:[]]];
}
let catalogueSource:(ReturnType<typeof catalogueForm>&{clear:()=>void})|null=null;
let tagSource:HTMLElement|null=null;
function clearSourceRoute(page:string,kind:string,id:string){
  if(view===page&&sectionOf(routeParts[0]||'')===kind&&routeQuery.get('open')===id){routeQuery.delete('open');
    if(location.hash===routeSeen){history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;}}
}
/** Keep the exact record open across its Save refresh, and clear only the address this form owns when it closes. */
function openCatalogueSource(target:HTMLElement){
  const page=view,kind=sectionOf(routeParts[0]||''),id=routeQuery.get('open');
  if(!id||routeParts.length!==1||!catalogueSources().some(([p,kinds])=>p===page&&kinds.includes(kind))||catalogueSource?.dialog.isConnected)return;
  const row=find(kind,id);
  if(!row){target.append(note('This record is not available. Choose one from the list.','warning'));return;}
  if(kind==='tags'){
    if(tagSource?.isConnected)return;
    let opened:HTMLElement|null=null;
    tagEditor(ctx(),row,permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR),undefined,{
      opened:panel=>{tagSource=opened=panel;},
      closed:leaving=>{if(tagSource!==opened)return;tagSource=null;if(!leaving)clearSourceRoute(page,kind,id);}});
    return;
  }
  const form=catalogueForm(ctx(),kind,row);
  const clear=()=>{
    if(catalogueSource?.dialog!==form.dialog)return;catalogueSource=null;
    clearSourceRoute(page,kind,id);
  };
  catalogueSource={...form,clear};form.dialog.addEventListener('close',clear,{once:true});
}
/** Configuration. Admins: the grouped section list; coordinators: the steps they can do and Tags (rules and filters
 * name #tags of any kind). The Setup guide is always first: once setup is done it is Data health (C-33), so what is
 * missing stays one click away. Until setup is done (or a section is picked), Configuration opens on the guide. */
async function configurationPage(target:HTMLElement){
  const sections=configSections();
  if(!routeParts.length&&!configPicked&&sections.includes('setup')&&!setupComplete()){routeParts=['setup'];history.replaceState(null,'',routeHash(view,routeParts));}
  await renderCatalogue(target,sections);}
/** Configuration › Setup guide: the steps to get the department ready to plan (coordinators see the ones they can do). */
async function setupGuide(t:HTMLElement){if(permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR))rotaTotal=(await get<{total:number}>('/api/workspaces?limit=1&offset=0')).total;const work=rotaCount(),open=setupOpeners();
  // SW3-D-11: ranks sharing a code, the server's own check (kept for the nav's count; a failed read lists none).
  if(permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.AUDITOR))rankCodeClashes=(await get<{duplicates:RankCodeClash[]}>('/api/catalogues/categories/duplicate-codes').catch(()=>({duplicates:[]}))).duplicates;
  setupGuidePage(t,setupSteps(ctx(),open,work),dataHealth(ctx(),open,rankCodeClashes),allowed('system')&&tabsFor('system').some(x=>x.key==='imports')?()=>navigate('system/imports'):undefined);}
/** A settings page's Discard: its values are drawn again from the server's (the unsaved guard is not asked). */
const discardSettings=()=>{leaveCheck=null;return renderPage();};
/** A settings save refused because someone else saved since its form was drawn (STALE_REVISION on the form's drawn base,
 *  settings_base.ts): said in `slot` with Reload, as Notifications and Sign-in settings say theirs. False: another error. */
function settingsMoved(e:unknown,slot:HTMLElement,what:string,reload:()=>void){
  if(!(e instanceof ApiError&&e.code==='STALE_REVISION'))return false;
  const n=note(`Someone else changed the ${what}. Reload to see them; your changes here are not saved.`,'warning');
  n.append(' ',button('Reload',reload,'secondary small'));
  slot.replaceChildren(n);slot.scrollIntoView({block:'nearest'});return true;}
/** A settings save patches the session from the command's reply (the department and organisation it returns) and draws
 *  the page again; nothing else is read (the lists, inbox and roles cannot have changed). */
let keptAccess:Row|null=null,accessRead:Promise<Row>|null=null;
// Reads that belong to the signed-in account: a sign-out or account switch (clearSessionState) forgets them, so the next
// account never sees the last one's Access, leave, checks or counts.
onSessionReset(()=>{keptAccess=null;accessRead=null;baselineDriftCache.clear();rankCodeClashes=[];forgetNames();recheckWait=null;academicCache.clear();liveChecks.clear();
  pillCounts.clear();leaveRead=null;draftChoice=null;});
async function settingsSaved(result:Row,access?:Row|null){
  if(result.organisation)me!.organisation=result.organisation;if(result.department)me!.department=result.department;
  applyDisplay();leaveCheck=null;keptAccess=access||null;await renderPage();}
/** The Department settings tab last shown: a save or Discard draws the page again on the same tab. */
let departmentTab='general';
/** Configuration › Department settings: names, timezone, week start, swap limits and sign-in session limits. */
async function departmentSettingsPage(target:HTMLElement){
  // SW3-D-12: a field's plain explanation stays its accessible description (F12) and shows behind ⓘ beside its label,
  // not as a sentence under every field. What warns (the routes could not be read) stays on screen.
  let abouts=0;
  const about=(wrap:HTMLElement,control:HTMLElement|null,text:string,shown=false)=>{const id=`dept-help-${++abouts}`;
    wrap.append(el('small',{class:shown?'hint':'sr-only',id},text));
    if(!shown){const label=wrap.querySelector(':scope>label,:scope>legend,:scope>.field-label');label?.after(helpButton('About '+(label?.textContent||'this setting'),text,'field-help'));}
    control?.setAttribute('aria-describedby',[control.getAttribute('aria-describedby'),id].filter(Boolean).join(' '));};
  await ensureCatalogues();  // opening hours, for the working-days choice
  // /api/access only feeds the Sign-in sessions panel: unread, that panel says so with Retry and the form still opens (C-22).
  let accessUnread='';
  const org=me!.organisation,access:Row=keptAccess||await get<Row>('/api/access').catch(e=>{if(sessionChanged(e))throw e;accessUnread=(e as Error).message;return {} as Row;});let dept=me!.department;keptAccess=null;
  // Every route page: the configured swap route must stay choosable even when it is not on the first page.
  const allRoutes:Row[]=[];let routesLoaded=true;
  try{for(let offset=0;;offset+=100){const page=await get<{routes:Row[];has_more:boolean}>(`/api/review-routes?limit=100&offset=${offset}`);allRoutes.push(...page.routes);if(!page.has_more)break;}}
  catch{routesLoaded=false;}
  const routes=allRoutes.filter(r=>r.purpose==='swap'&&r.status==='active');
  // Bulk changes: the department's effective limits (its own, else the deployment's), labels and bounds from the contract.
  // The bulk limits as saved; unread (the request failed), they are shown read only and never sent with a save.
  let bulkUnread='';
  const bulkNow=(await get<{settings:Record<string,number>}>('/api/bulk/settings').catch(e=>{if(sessionChanged(e))throw e;bulkUnread=(e as Error).message;return {settings:{} as Record<string,number>};})).settings;
  const bulkInputs=Object.entries(BULK_SETTING_FIELDS).map(([key,f])=>{const x=field(f.label,'number',bulkUnread?'':String(bulkNow[key]??f.default),{required:!bulkUnread,min:f.minimum,max:f.maximum,step:1,...(bulkUnread?{disabled:true}:{})});
    about(x.wrap,x.input,f.description);return [key,x] as const;});
  const bulkValue=()=>Object.fromEntries(bulkInputs.map(([k,x])=>[k,Number(x.input.value)]));
  // Most applied directly can never be more than most items per change (the schema's rule, said on the field).
  const directCheck=()=>{const v=bulkValue(),direct=bulkInputs.find(([k])=>k==='bulk_direct_max')![1].input;
    direct.setCustomValidity(v.bulk_direct_max>v.bulk_max_items?`At most ${BULK_SETTING_FIELDS.bulk_max_items.label.toLowerCase()} (${v.bulk_max_items}).`:'');};
  for(const [,x] of bulkInputs)x.input.addEventListener('input',directCheck);
  const departmentName=field('Department name','text',dept.name,{required:true,maxLength:160}),organisationName=field('Organisation name','text',org.name,{required:true,maxLength:160});
  // Every IANA time zone this browser knows, searchable in the shared picker; the saved one is always a choice.
  const ianaZones=((Intl as unknown as {supportedValuesOf?:(k:string)=>string[]}).supportedValuesOf?.('timeZone'))||[];
  const timezone=select('Scheduling timezone',[...new Set([org.timezone,...ianaZones,'UTC'])].map(z=>({value:z,label:z.replaceAll('_',' '),group:z.includes('/')?z.split('/')[0]:'Other',keywords:z})),org.timezone);
  const weekStart=select('First day of week',weekdayOptions(),String(org.week_start));  // the same day order as every other day list
  // The weekend calendars shade (picker, Schedule, planner); rules keep their own weekend for counting.
  const weekend=multi('Weekend days',weekdayOptions(),((dept as Row).weekend_days||[4,5]).map(String),'No weekend');
  // What planners see about someone's leave on issues, the Why tab and auto-schedule (the server sends only this much).
  const leave=select('Leave on planning issues',LEAVE_DISPLAY_CHOICES,String((dept as Row).leave_reason_display||'type'));
  about(leave.wrap,leave.input,'For coordinators, approvers, programme leads and auditors. Staff never see others’ leave reasons.');
  // Working days for prorating a rule's minimum or target when only part of its period counts (quotas.py): the
  // department's opening hours, chosen here (never guessed from a name). Sent only when changed; active ones only.
  const calendarNow=(dept as Row).operating_calendar_id?String((dept as Row).operating_calendar_id):'';
  const calendarChoices=choices(rows('operating_calendars').filter(r=>r.active!==false||r.id===calendarNow));
  const workCalendar=select('Working days for prorated targets',calendarChoices,calendarNow,false,'Department weekend days');
  // F-C4: a plain explanation, so contextual help beside the label (as Recurring sessions' dates), not a paragraph.
  workCalendar.wrap.querySelector('label')?.after(el('button',{type:'button',class:'icon-button bare field-help','aria-label':'How prorating works','data-help':'','data-tip':'When only part of a rule period is in a rota (a start date, a rotation), its minimum or target is prorated by the working days of these opening hours, holidays included. Days a person is away (leave or absence) are not working days for them.'},icon('help')));
  // Swaps: who approves them and which steps run by themselves (a dedicated route is set up under Review routes).
  // The saved choice is always an option (also when it is no longer active or the routes could not be read) and is sent
  // only when changed, so saving an unrelated setting neither resets swaps to the rota review route nor fails on a retired one.
  const d=dept as Row,current=d.swap_review_route_id?String(d.swap_review_route_id):'';
  const routeChoices=routes.map(r=>({value:String(r.id),label:String(r.name||'Review route')}));
  if(current&&!routeChoices.some(c=>c.value===current)){const r=allRoutes.find(x=>String(x.id)===current);
    routeChoices.unshift({value:current,label:r?`${r.name||'Review route'} (${r.status==='active'?'current':'no longer active'})`:'Current swap route'});}
  // Each swap hint is its control's accessible description (WCAG 1.3.1), not only text placed beside it.
  const hint=about;
  const swapRoute=select('Who approves swaps',routeChoices,current,false,'The rota review route');
  swapRoute.input.disabled=!routesLoaded;
  hint(swapRoute.wrap,swapRoute.input,!routesLoaded?`${couldntLoad('the review routes')} The current choice is kept. Reload the page to change it.`
    :routes.length?'A swap route reviews swap amendments only.':'Add a review route for swap amendments only under Review routes to choose it here.',!routesLoaded);
  const autoPrepare=check('Prepare automatically when both agree',d.swap_auto_prepare??true,'Submits the rota change for approval as soon as both people agree. If it cannot be prepared, a coordinator is asked to.');
  const autoPublish=check('Publish automatically after final approval',d.swap_auto_publish??true,'Only for swap amendments. Off, a coordinator publishes the approved change.');
  // Which offers staff may make; offers already made can still complete.
  const swapKinds=multi('Swap kinds allowed',SWAP_KINDS.map(k=>({value:k,label:SWAP_KIND_WORDS[k]})),(d.swap_kinds_allowed??[...SWAP_KINDS]) as string[],'No new swaps');
  hint(swapKinds.wrap,swapKinds.wrap.matches('fieldset')?swapKinds.wrap:swapKinds.wrap.querySelector('button'),'Choose none to stop new swap offers. Offers already made can still complete.');
  const openOffers=check('Open swap offers',d.swap_open_offers??true,'Staff may offer a duty to every eligible colleague without naming one; the first to answer takes it, and the proposer still confirms.');
  // Swap limits: at least the contract minimum; no policy maximum (the contract ceiling is integer storage).
  const swapWindow=field('Swap offer open for (days)','number',d.swap_offer_window_days??SWAP_LIMITS.offer_window_days,{required:true,min:SWAP_LIMITS.minimum,max:SWAP_LIMITS.maximum,step:1});
  hint(swapWindow.wrap,swapWindow.input,'The longest a new swap offer can wait for a reply. It always ends before the duty starts.');
  const swapCap=field('Open swap offers per person','number',d.swap_open_offer_cap??SWAP_LIMITS.open_offer_cap,{required:true,min:SWAP_LIMITS.minimum,max:SWAP_LIMITS.maximum,step:1});
  hint(swapCap.wrap,swapCap.input,'Offers waiting for a reply, agreed or in review count until they close.');
  const scoreCheck=check('Score checking',d.score_check_enabled??false,'Allows rota-file checking and comparison. AI reading needs separate provider settings and consent.');
  // Turning it on is a Developer's decision (the server refuses anyone else); any admin may turn it off.
  if(!scoreCheck.input.checked&&!may('access.system_roles'))scoreCheck.input.disabled=true;
  const eventHorizon=field('Event horizon · days','number',dept.event_horizon_days,{required:true,min:7,max:EVENT_HORIZON_MAX_DAYS,step:1});
  const receiptWindow=field('Event receipt window · days','number',dept.event_receipt_window_days,{required:true,min:1,max:EVENT_HORIZON_MAX_DAYS,step:1});
  about(eventHorizon.wrap,eventHorizon.input,'Repeat events appear this many days ahead. The window advances daily.');
  about(receiptWindow.wrap,receiptWindow.input,'Daily checks prepare receipts this many days ahead. Delivery, reading and responses record receipts when needed.');
  // Announcements: when a notice that starts on a later day reaches inboxes, and automatic reminders to whoever has not read
  // (or, when required, acknowledged) it. Blank days means no automatic reminders; the interval also spaces manual ones.
  const sendTime=field('Announcements reach inboxes at','time',d.announcement_send_time||'07:00',{required:true});
  about(sendTime.wrap,sendTime.input,'For announcements that start on a later day, in the scheduling timezone.');
  const reminderDays=field('Remind people who have not read after (days)','number',d.announcement_auto_reminder_days??'',{min:1,step:1,placeholder:'No automatic reminders'});
  const reminderGap=field('Hours between reminders','number',d.announcement_reminder_interval_hours??24,{required:true,min:1,step:1});
  // The setting can raise the safety retirement minimum, which also applies at the default of one.
  const retireApprovals=field('Approvals to retire or restore a rule','number',d.policy_retire_approvals_required??1,{required:true,min:1,max:MAX_APPROVALS_REQUIRED,step:1});
  retireApprovals.input.setAttribute('aria-describedby','safety-retirement-minimum');
  retireApprovals.wrap.append(el('small',{class:'hint',id:'safety-retirement-minimum'},SAFETY_RETIREMENT_NOTICE));
  // One form in tabs, the same layout as Auto-schedule & scoring (C-06): each tab a grid across the panel's width.
  const grid=(...children:Node[])=>el('div',{class:'form-grid'},...children);
  const panels:[string,string,HTMLElement][]=[
    ['general','General',grid(departmentName.wrap,organisationName.wrap,timezone.wrap,weekStart.wrap,weekend.wrap,workCalendar.wrap,leave.wrap)],
    ['swaps','Swaps',grid(swapKinds.wrap,openOffers.wrap,swapRoute.wrap,autoPrepare.wrap,autoPublish.wrap,swapWindow.wrap,swapCap.wrap)],
    ['announcements',TERMS.announcements,grid(sendTime.wrap,reminderDays.wrap,reminderGap.wrap)],
    ['governance','Governance',grid(retireApprovals.wrap)],
    ['advanced','Advanced',grid(scoreCheck.wrap,el('h3',{class:'span-all'},'Events'),eventHorizon.wrap,receiptWindow.wrap,
      el('h3',{class:'span-all'},'Bulk changes'),...(bulkUnread?[el('div',{class:'span-all'},note(`${couldntLoad('the bulk limits')} ${bulkUnread} They keep their saved values; reload to change them.`,'warning'))]:[]),...bulkInputs.map(([,x])=>x.wrap))]];
  const sections=tabs('Department settings',panels.map(([key,label,panel])=>[key,label,()=>panel]),departmentTab,k=>{departmentTab=k;});
  const form=el('form',{class:'settings-form',novalidate:true},sections);
  // A wrong value on a tab not shown: open its tab so the browser can point at it.
  const invalid=(p:HTMLElement)=>p.querySelector<HTMLInputElement>(':is(input,select,textarea):invalid');
  const valid=()=>{const at=panels.find(([,,p])=>invalid(p));if(!at)return true;
    sections.querySelector<HTMLElement>(`[role=tab][id$="-${at[0]}-tab"]`)?.click();return invalid(at[2])!.reportValidity();};
  const save=button('Save settings',()=>{if(!valid())return;confirmAction('Save department settings','Drafts are checked again. Published rotas keep their dates and timezone; opening hours keep their own timezone.',async reason=>{
    await settingsSaved(await command('department.settings',{department_name:departmentName.input.value,department_revision:dept.revision,organisation_name:organisationName.input.value,timezone:timezone.input.value,week_start:Number(weekStart.input.value),weekend_days:weekend.values().map(Number),leave_reason_display:leave.input.value,...(workCalendar.input.value!==calendarNow?{operating_calendar_id:workCalendar.input.value||null}:{}),...(swapRoute.input.value!==current?{swap_review_route_id:swapRoute.input.value||null}:{}),swap_auto_prepare:autoPrepare.input.checked,swap_auto_publish:autoPublish.input.checked,swap_kinds_allowed:swapKinds.values(),swap_open_offers:openOffers.input.checked,
      swap_offer_window_days:Number(swapWindow.input.value),swap_open_offer_cap:Number(swapCap.input.value),
      announcement_send_time:sendTime.input.value,announcement_auto_reminder_days:reminderDays.input.value?Number(reminderDays.input.value):null,announcement_reminder_interval_hours:Number(reminderGap.input.value),
      score_check_enabled:scoreCheck.input.checked,event_horizon_days:Number(eventHorizon.input.value),event_receipt_window_days:Number(receiptWindow.input.value),
      policy_retire_approvals_required:Number(retireApprovals.input.value),
      // Sent only when changed: saving another setting keeps the deployment's limits rather than copying them here.
      ...(!bulkUnread&&Object.keys(bulkNow).length&&bulkInputs.some(([k,x])=>Number(x.input.value)!==bulkNow[k])?{bulk_settings:bulkValue()}:{}),reason},dept.id,org.revision),accessUnread?null:access);},'Save settings');},'primary',{disabled:true,why:TERMS.nothingChanged});
  form.addEventListener('input',()=>{save.disabled=false;});form.addEventListener('change',()=>{save.disabled=false;});
  form.addEventListener('submit',e=>{e.preventDefault();save.click();});leaveCheck=()=>!save.disabled;

  // Independent saves (and their Undo) update their own panels, keeping the unsaved department form and its guard.
  const independentSaved=async(result:Row)=>{if(result.department)me!.department=dept=result.department;
    if(result.sessions)access.sessions=result.sessions;applyDisplay();
    const nextSessions=access.sessions?sessionsPanel(dept,access.sessions,independentSaved):unreadSessions(),nextRules=ruleCataloguePanel(independentSaved);
    sessions.replaceWith(nextSessions);sessions=nextSessions;rules.replaceWith(nextRules);rules=nextRules;
    const nextWhere=whereaboutsPanel(independentSaved);whereabouts.replaceWith(nextWhere);whereabouts=nextWhere;};
  const unreadSessions=():HTMLElement=>{const retry=()=>void get<Row>('/api/access').then(next=>{access.sessions=next.sessions;accessUnread='';
      const panel=sessionsPanel(dept,access.sessions,independentSaved);sessions.replaceWith(panel);sessions=panel;},
      e=>{if(sessionChanged(e))return;accessUnread=(e as Error).message;const panel=unreadSessions();sessions.replaceWith(panel);sessions=panel;});
    return el('section',{class:'panel'},sectionHead('Sign-in sessions',''),readFailed('the sign-in session settings',new Error(accessUnread),retry));};
  let sessions=accessUnread?unreadSessions():sessionsPanel(dept,access.sessions,independentSaved),rules=ruleCataloguePanel(independentSaved),whereabouts=whereaboutsPanel(independentSaved),branding=brandingPanel();
  undoInPlace(async done=>{if(!form.isConnected||done.some(d=>!['department.sessions','department.display','department.display.restore','branding.undo'].includes(d.kind)))return false;
    for(const d of done){if(d.kind==='branding.undo'){const next=brandingPanel();branding.replaceWith(next);branding=next;}else await independentSaved(d.result);}return true;});
  target.append(sectionHead(kindLabel('department'),''),el('section',{class:'panel'},form),branding,sessions,rules,whereabouts,saveBar(save,discardSettings));

}
/** The Rules page as the rule catalogue (off until it is proven on this department's rules): saved on its own. */
function ruleCataloguePanel(saved:(result:Row)=>Promise<void>){
  const on=ruleCatalogueOn(),box=check('Show the Rules page as the rule catalogue',on,'One rule per idea, with lines for who, where and when. Rules not in the catalogue keep applying as they are.');
  box.input.addEventListener('change',()=>{const want=box.input.checked;box.input.checked=on;
    confirmAction(want?'Show the rule catalogue':'Show the current Rules page',want?'Everyone who opens Rules sees the catalogue. Nothing about the rules in force changes.':'Rules shows the current page again. Catalogue rules keep applying.',async reason=>{
      const d=me!.department as Row,result=await command('department.display',{rule_catalogue:want,reason},d.id,d.revision);notify(want?'The Rules page now shows the rule catalogue.':'The Rules page shows the current page again.');await saved(result);},'Save');});
  return el('section',{class:'panel'},el('h3',{},'Rules page'),el('div',{class:'switch-group'},box.wrap));
}
/** People › Where is everyone: which areas a month's "worked in" names (a versioned display setting, with Undo).
 *  Defaults and limits are the contract's (WhereaboutsSettings). */
function whereaboutsPanel(saved:(result:Row)=>Promise<void>){
  // The department these two values were read from: the save sends its revision (settings_base.ts).
  const base=drawnBase(me!.department),conflict=el('div',{});
  const now={...WHEREABOUTS_DEFAULTS,...((me!.department as Row).display_settings?.whereabouts||{})} as {area_share:number;max_areas:number};
  const [s0,s1]=WHEREABOUTS_LIMITS.area_share,[m0,m1]=WHEREABOUTS_LIMITS.max_areas;
  // A column is a month at 9 or 12 months and a week at 3 or 6 months.
  const share=field('Share of a column’s duties that names an area (%)','number',now.area_share,{required:true,min:s0,max:s1,step:1});
  const most=field('Areas named at most','number',now.max_areas,{required:true,min:m0,max:m1,step:1});
  const save=button('Save',()=>{if(!share.input.reportValidity()||!most.input.reportValidity())return;
    const want={area_share:Number(share.input.value),max_areas:Number(most.input.value)};
    confirmAction('Save Where is everyone',`Each month or week names the areas with at least ${want.area_share}% of its duties, at most ${want.max_areas}.`,async reason=>{
      // Unsaved department settings on the same page are asked about before the page is read again.
      try{const result=await command('department.display',{whereabouts:want,reason},base.id,base.revision);notify('Where is everyone settings saved.');await saved(result);}
      catch(e){if(!settingsMoved(e,conflict,'Where is everyone settings',()=>{leaving(()=>void refresh(),'the latest settings');}))throw e;}},'Save');},'secondary small');
  return el('section',{class:'panel'},el('h3',{},'Where is everyone'),conflict,el('div',{class:'vs-fields'},share.wrap,most.wrap),el('div',{class:'actions'},save));
}
/** Configuration › Auto-schedule & scoring: the department defaults every run starts from (runs can override them). */
/** The draft's Score review, for "Why this score" (who may open it: a coordinator or an admin). */
const reviewScores=(id:string)=>permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN)?()=>navigate(`configuration/auto_schedule?tab=score&draft=${encodeURIComponent(id)}`):undefined;
/** Score review (score_review.ts) for this page: the department's drafts, the open one first (or ?draft=). */
function scoreReviewTab():HTMLElement{
  // Every wait ends (DL-045): the review, or a failure or timeout with Retry, in the skeleton's place.
  const box=el('div',{});const load=()=>{box.replaceChildren(skeleton('panel','Loading the score review…'));void (async()=>{
    const list=(await get<{workspaces:Row[]}>('/api/workspaces?limit=50&offset=0')).workspaces.filter(w=>w.status==='draft'&&!w.archived_at);
    // A failure ends in this box (the catch below), so its Retry reloads the box and keeps focus there (DL-045).
    const node=await scoreReview({drafts:list.map(w=>({id:String(w.id),name:String(w.name)})),draft:routeQuery.get('draft')||workspaceId,label:namer(ctx()).label,
      canPropose:permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN),
      chosen:id=>{routeQuery.set('draft',id);routeQuery.set('tab','score');history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;void renderPage();}},
      ()=>void renderPage());
    if(alive(box))box.replaceChildren(node);})().catch(e=>{if(alive(box)&&!sessionChanged(e))box.replaceChildren(readFailed('the score review',e,load));});};
  load();return box;
}
/** "Weights 35 / 25 / 40 / 10 / 5 · tiers 1 / 3 / 10 / 30" in the contract's order (SCORE_WEIGHT_LABELS, STRENGTH_LABELS). */
const scoreSettingsWords=(d:Row)=>`${Object.keys(SCORE_WEIGHT_LABELS).map(k=>d.weights?.[k]??'–').join(' / ')} · tiers ${Object.keys(STRENGTH_LABELS).map(k=>d.strengths?.[k]??'–').join(' / ')}`;
async function autoScheduleSettingsPage(target:HTMLElement){
  // A coordinator's section is the Score review (their rule proposals); department settings are an admin's.
  if(!permits(CAPABILITY.ADMIN)){target.append(sectionHead(kindLabel('auto_schedule'),''),el('section',{class:'panel'},scoreReviewTab()));return;}
  const factory=(await get('/api/policies/capabilities')).generation_defaults as GenerationOptions,org=me!.organisation,dept=me!.department;
  let value:GenerationOptions|null=null,kindsChanged=false,motion:AnimationSettings|null=null,controlsChanged=false;
  // The kinds of wish staff may ask for sit with the wish points (Staff preferences); sent only when changed.
  const wishes=wishTypeSettingsFields((dept as Row).wish_settings?.types);
  // Existing rotas are scored again only when the score weights change: say so only then.
  const saved={...factory,...dept.generation_defaults} as GenerationOptions;
  const weightsChanged=()=>{const now=(value?.scoring?.weights||{}) as Record<string,number>,was=(saved.scoring?.weights||{}) as Record<string,number>;
    return !!value&&Object.keys({...now,...was}).some(k=>Number(now[k])!==Number(was[k]));};
  // Add time's suggested step on a running search: a department run control (generation_controls), not a solve input.
  const savedStep=(dept as Row).display_settings?.generation_controls?.extension_minutes??GENERATION_CONTROL_DEFAULTS.extension_minutes;
  // Its words and help are the contract's (GENERATION_TIME_FIELDS), in the "?" beside its label like the other times.
  const step=timeField('extension_minutes',savedStep);
  // A wrong step with Timing not shown: open that tab so the browser can point at it.
  const stepValid=()=>step.input.checkValidity()||(target.querySelector<HTMLElement>('[role=tab][id$="-timing-tab"]')?.click(),step.input.reportValidity());
  const save=button('Save defaults',()=>{if(!options.valid()||!animation.valid()||!stepValid()||!value&&!kindsChanged&&!motion&&!controlsChanged)return;
    confirmAction('Save auto-schedule defaults',[value?'New auto-schedule runs start from these.':'',kindsChanged?'New wishes follow the kinds of wish set here.':'',motion?'Auto-schedule shows its changes this way from the next run.':'',
      controlsChanged?`Add time suggests ${duration(step.value()*60)}.`:'',
      weightsChanged()?'Scores of existing rotas are recalculated with the new weights.':''].filter(Boolean).join(' '),async reason=>{
    await settingsSaved(await command('department.settings',{department_name:dept.name,department_revision:dept.revision,organisation_name:org.name,timezone:org.timezone,week_start:org.week_start,
      ...(value?{generation_defaults:value}:{}),...(kindsChanged?{wish_settings:wishes.value()}:{}),...(motion?{autoschedule_animation:motion}:{}),
      ...(controlsChanged?{generation_controls:{extension_minutes:step.value()}}:{}),reason},dept.id,org.revision));},'Save defaults');},'primary',{disabled:true,why:TERMS.nothingChanged});
  const animation=animationFields(animationSettings(dept),v=>{motion=v;save.disabled=false;});
  for(const type of ['input','change'])wishes.panel.addEventListener(type,()=>{kindsChanged=true;save.disabled=false;});
  const options=generationOptionsFields({...factory,...dept.generation_defaults},v=>{value=v;save.disabled=false;},factory);leaveCheck=()=>!save.disabled;
  options.timing.querySelector('fieldset')!.append(step.wrap);
  step.input.addEventListener('input',()=>{controlsChanged=step.value()!==savedStep;save.disabled=false;});
  // The score's weights and tiers are versioned (score_settings_versions): History lists them with Restore (an older
  // version comes back as a new one, audited, with its own Undo).
  const scoreHistory=button('History',()=>{let latest:Row|null=null;historyDialog('Score settings history','Weights · tiers',async offset=>{
      const r=await get<{versions:Row[];has_more:boolean}>(`/api/score-settings?history=true&limit=100&offset=${offset}`);if(!offset)latest=r.versions[0]||null;
      return {more:r.has_more,versions:r.versions.map(v=>({version:Number(v.revision),saved:formatDate(String(v.effective_from||v.created_at),zone()),actor_id:v.actor_id||undefined,reason:String(v.reason||''),
        value:scoreSettingsWords(v.definition||{}),restore:v.id===latest?.id?undefined:(close:()=>void)=>confirmAction(`Restore version ${v.revision}`,'Scores of existing rotas are recalculated with these weights and tiers.',async reason=>{
          const reply=await command('score.settings.rollback',{version_id:v.id,reason},dept.id,Number(latest!.revision));close();notify('Score settings restored.');await settingsSaved(reply);},'Restore')}))};},
    'Each saved change of the score’s part weights and tier values.');},'ghost small');
  const tabbed=options.tabbed([wishes.panel],[['animation','Animation',()=>animation.root]],[['score','Score review',scoreReviewTab]],routeQuery.get('tab')||undefined);
  tabbed.querySelector('.auto-options-head')?.append(scoreHistory);
  target.append(sectionHead(kindLabel('auto_schedule'),''),el('section',{class:'panel'},tabbed),saveBar(save,discardSettings));
}
/** Names, short codes and tags: how people appear on dense views. Codes are automatic and editable per person. */
function namesPage(target:HTMLElement){
  const isAdmin=permits(CAPABILITY.ADMIN),dept:Row=(me!.department as Row).display_settings||{};
  // The department the draft below was read from: Save defaults sends its revision (settings_base.ts).
  const base=drawnBase(me!.department),conflict=el('div',{});
  const draft={names_default:dept.names_default||'full',ignore_al_prefix:dept.ignore_al_prefix!==false,avoid_duplicate_codes:dept.avoid_duplicate_codes!==false,tags:[...((dept.tags||[]) as {label:string;name:string}[])]};
  // With person labels on (D75), the department's own Person label and its title and particle lists (versioned rows).
  const designed=isAdmin&&labelsDesigned();
  const labelDraft={person_label:(dept.person_label||NAME_LABEL[(dept.names_default||'full') as NameMode]) as PersonLabel,
    honorifics:[...((dept.honorifics as string[]|undefined)||HONORIFIC_DEFAULTS)],name_particles:[...((dept.name_particles as string[]|undefined)||NAME_PARTICLE_DEFAULTS)]};
  const people=rows('people').filter(p=>p.active!==false);
  const used=new Map<string,number>();for(const p of people){const c=shortCode(p);used.set(c,(used.get(c)||0)+1);}
  const first=people[0],modes:[NameMode,string,string][]=[['code','Short code',first?shortCode(first):'—'],['short','Initial + family name',first?personLabel(first,NAME_LABEL.short):'—'],['full','Full name',first?fullNameOf(first):'—'],['number','Staff number',String(first?.staff_number||'—')]];
  const modeBox=el('div',{class:'name-modes',role:'radiogroup','aria-label':'Show names as'},...modes.map(([value,label,eg])=>{
    const r=el('input',{type:'radio',name:'name-mode',value}) as HTMLInputElement;r.checked=(isAdmin?dept.names_default||'full':nameMode())===value;r.addEventListener('change',()=>{if(isAdmin){draft.names_default=value;touch();}else{setNameMode(value);notify('Names will show as '+label.toLowerCase()+' on the schedule.');}});
    return el('label',{class:'name-mode'},r,el('span',{},el('strong',{},label),el('small',{},eg)));}));
  const canEdit=permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR);
  // Department defaults (admin): saved from the unsaved-changes bar, like every settings page.
  const saveDefaults=isAdmin?button('Save defaults',()=>confirmAction('Save display defaults','Applies to everyone who has not picked their own view. Codes that were set by hand stay as they are.',async reason=>{
    try{const result=await command('department.display',{...draft,...(designed?labelDraft:{}),reason},base.id,base.revision);notify('Department display defaults saved.');await settingsSaved(result);}
    catch(e){if(!settingsMoved(e,conflict,'display defaults',()=>{leaveCheck=null;void refresh();}))throw e;}},'Save defaults',undefined,true),'primary',{disabled:true,why:TERMS.nothingChanged}):undefined;
  function touch(){if(saveDefaults)saveDefaults.disabled=false;}
  if(saveDefaults)leaveCheck=()=>!saveDefaults.disabled;
  // The shared Person label editor (view_settings.ts labelBody), for the department's default.
  const labelBox=el('div',{class:'names-label'}),sample=people[0];
  const drawLabel=(focus='')=>{labelBox.replaceChildren();labelBody(labelBox,{value:()=>labelDraft.person_label,
    set:(l,f)=>{labelDraft.person_label=l;touch();drawLabel(f);},part:(part,l)=>sample?labelPart(sample,part,l.honorific,labelFacts(sample)):'',
    preview:l=>sample?personLabel(sample,l,labelFacts(sample)):'—'});if(focus)labelBox.querySelector<HTMLElement>(focus)?.focus();};
  if(designed)drawLabel();
  const listField=(label:string,key:'honorifics'|'name_particles')=>{const f=field(label,'text',labelDraft[key].join(', '));
    f.input.addEventListener('change',()=>{labelDraft[key]=f.input.value.split(',').map(x=>x.trim()).filter(Boolean);touch();});return f.wrap;};
  const labelPanel=el('section',{class:'panel'},el('h3',{},'Person label'),isAdmin?personLabelsPanel():'',labelBox,el('div',{class:'vs-fields'},listField('Titles','honorifics'),listField('Family-name particles','name_particles')));
  target.append(sectionHead(kindLabel('names'),''),conflict,
    el('div',{class:'names-top'},
      // maintenance: the switch to design the department's Person label sits with the names default it replaces, not below the people list.
      designed?labelPanel:el('section',{class:'panel'},el('h3',{},isAdmin?'Default display for the department':'Show names as'),modeBox,el('p',{class:'hint'},isAdmin?'Everyone sees this until they pick their own view on the schedule toolbar.':ownNameMode()?'This is your own choice. It overrides the department default.':'You are using the department default. Pick one to use your own.'),isAdmin?personLabelsPanel():''),
      el('section',{class:'panel'},el('h3',{},'How short codes are made'),el('ul',{class:'names-rules'},el('li',{},'First, middle and family initial when there are three names; otherwise first initial and two letters of the family name.'),el('li',{},'“Dr.” and an “Al” before the family name are ignored.'),el('li',{},'Duplicates get a later letter of the family name, then a number.'),el('li',{},'A saved code always wins over the automatic one.')),
        isAdmin?el('div',{class:'names-toggles'},ruleToggle('Ignore “Al” at the start of family names','ignore_al_prefix'),ruleToggle('Avoid duplicate codes automatically','avoid_duplicate_codes')):'',
        people.length?el('p',{class:'names-eg'},...people.slice(0,2).flatMap((p,i)=>[i?' · ':'',el('span',{class:'code-chip'},shortCode(p)),' '+fullNameOf(p)])):'')));
  function ruleToggle(label:string,key:'ignore_al_prefix'|'avoid_duplicate_codes'){const c=check(label,draft[key]);c.input.addEventListener('change',()=>{draft[key]=c.input.checked;touch();});return c.wrap;}
  // One person editor (People): a code opens that person's record, where it is changed like any other detail.
  const codeInput=(p:Row)=>canEdit?button(shortCode(p),()=>personDetails(p),'',{class:'code-chip code-open','aria-label':'Short code for '+fullNameOf(p),'data-tip':'Open '+fullNameOf(p)+' to change it'})
    :el('span',{class:'code-chip'},shortCode(p));
  const codeState=(p:Row)=>(used.get(shortCode(p))||0)>1?'Needs a look':p.short_code?'Edited':'Automatic';
  const table=dataTable<Row>({id:'names',noun:['person','people'],rows:people,rowId:p=>p.id,searchLabel:'Find people',searchPlaceholder:'Name, code or staff number',
    columns:[
      {key:'name',label:'Name',text:p=>fullNameOf(p),cell:p=>el('strong',{},fullNameOf(p))},
      {key:'code',label:'Code used',text:p=>shortCode(p),cell:codeInput},
      {key:'staff',label:'Staff no.',text:p=>String(p.staff_number||''),cell:p=>el('span',{class:'ct-mono'},String(p.staff_number||'—'))},
      {key:'status',label:'Code status',facet:codeState,cell:p=>{const state=codeState(p);return state==='Automatic'?el('span',{}):statusBadge(state==='Needs a look'?'attention':'preview',state);}},
    ]});
  // Every collision counts: "SCL" on three people is 3 people sharing 1 code (each of their rows says Needs a look).
  const shared=[...used.values()].filter(n=>n>1),dups=shared.length,sharing=shared.reduce((a,n)=>a+n,0);
  target.append(el('section',{class:'panel'},el('div',{class:'names-head'},el('h3',{},'People'),dups?el('span',{class:'nt-status warn'},`${sharing} people share ${plural(dups,'code')}`):el('span',{class:'nt-status edited'},'Every code is unique')),table.root,
      canEdit?el('p',{class:'hint'},'Choose a code to open that person and change it. Clear it there to go back to the automatic code.'):null),
    ...(designed?[displayHistory()]:[]),
    saveDefaults?saveBar(saveDefaults,discardSettings):'');
}
/** The department's own Person label (D75, an admin flag, off until the department decides): saved on its own. */
function personLabelsPanel(){
  const on=labelsDesigned(),box=check('Design the department\u2019s person label',on);
  box.input.addEventListener('change',()=>{const want=box.input.checked;box.input.checked=on;
    confirmAction(want?'Design the person label':'Use the names default',want?'Names everywhere follow the department\u2019s Person label, until a person or a view picks its own.':'Names follow the names default again. The designed label stays in History.',async reason=>{
      const d=me!.department as Row,result=await command('department.display',{person_labels_enabled:want,reason},d.id,d.revision);notify(want?'Names now follow the department\u2019s Person label.':'Names follow the names default again.');await settingsSaved(result);},'Save');});
  return el('div',{class:'switch-group'},box.wrap);
}
/** Every saved version of the department's display settings (row-versioned, D75): who, when, what changed, and Restore,
 *  which saves that version again as a new one (nothing is overwritten). */
function displayHistory(){
  const box=el('section',{class:'panel'},el('h3',{},'History'),skeleton('lines','Loading history…'));
  void get<{versions:Row[]}>('/api/department/display/versions?limit=20').then(r=>{
    const restore=(v:Row)=>button('Restore',()=>confirmAction(`Restore revision ${v.revision}`,'Saves that version of the names and labels again. The current one stays in History.',async reason=>{
      const d=me!.department as Row,result=await command('department.display.restore',{version_id:v.id,reason},d.id,d.revision);notify(`Revision ${v.revision} restored.`);await settingsSaved(result);},'Restore'),'ghost small');
    box.replaceChildren(el('h3',{},'History'),r.versions.length?table(['Revision','Saved by','When','Changed',''],r.versions.map((v,i)=>[String(v.revision),String(v.actor_display_name||'An administrator'),
      formatDate(String(v.created_at).slice(0,10),zone()),((v.changed_fields||[]) as string[]).join(', ')||'—',i?restore(v):el('span',{class:'muted'},'Current')])):emptyLine('No saved versions yet.'));
  },e=>{if(!sessionChanged(e))box.replaceChildren(el('h3',{},'History'),readFailed('the history',e));});
  return box;
}
/** Extra table columns per configuration list: [heading, value, class]. */
function catalogueColumns(kind:string):[string,(row:Row)=>Node|string,string?][]{
  const mono='ct-mono';
  if(kind==='assignment_types')return [
    ['Session hours',r=>r.definition.default_timing?`${r.definition.default_timing.start_time}–${r.definition.default_timing.end_time}`:'Not set',mono],
    ['Staff needed',r=>staffNeeded(r)],
    ['Where',r=>((r.definition.allowed_location_type_ids||[]) as string[]).map(id=>name('location_types',id)).join(', ')||'Any location']];
  if(kind==='locations')return [['Type',r=>name('location_types',r.location_type_id)],['Capacity',r=>`${r.capacity} at a time${r.parent_id?' · in '+name('locations',r.parent_id):''}`]];
  if(kind==='resource_pools')return [['Units',r=>String(r.capacity),mono],['Where',r=>`${r.location_id?name('locations',r.location_id):'Department-wide'} · ${r.calendar_id?name('operating_calendars',r.calendar_id):'No opening hours'}`]];
  // A missing fact shows once, in its own column, in the warning tone (it blocks clean planning).
  const missing=(text:string)=>el('span',{class:'ct-warn'},icon('alert'),text);
  if(kind==='services')return [['Mode',r=>r.mode==='virtual'?'Virtual':r.mode==='physical'?'In person':missing('Not specified')],['Opening hours',r=>r.calendar_id?name('operating_calendars',r.calendar_id):missing('Not set')]];
  if(kind==='operating_calendars')return [['Timezone',r=>r.definition.timezone,mono],['Date overrides',r=>String(r.definition.overrides.length)]];
  if(kind==='leave_types')return [];  // its own editable columns (renderCatalogue); no effective dates
  // D68: name, kind, who sees it, one tap (the plain four columns, owner v2).
  if(kind==='contact_types')return [['Kind',r=>CONTACT_KINDS[r.kind as keyof typeof CONTACT_KINDS]||String(r.kind)],['Who can see it',r=>contactAudience(r.visibility)],['One-tap call',r=>r.one_tap?'Yes':'No']];
  if(kind==='date_sets')return dateSetColumns();  // a set has no effective dates: its entries say when
  if(kind==='rotation_templates')return [['Programme',r=>r.programme_id?name('programmes',r.programme_id):'Any programme'],
    ['Blocks',r=>`${(r.blocks||[]).length} · ${(r.blocks||[]).map((x:Row)=>x.length+(x.unit==='weeks'?' wk':' mo')).join(', ')}`]];
  if(kind==='programme_memberships')return [['Effective',r=>dateUnits([formatDate(r.starts_on),'– '+formatDate(lastIncludedDate(r.ends_on))])],
    ['Trainee',r=>r.external?badge('External rotator'+(r.home_programme?' · '+r.home_programme:''),'warn'):'Local']];
  return [['Effective',r=>dateUnits([r.starts_on?formatDate(r.starts_on):'From creation',...(r.ends_on?['– '+formatDate(lastIncludedDate(r.ends_on))]:[])])]];
}
/** Section addresses say what the section is called (#configuration/opening-hours), not an internal list name. The old
 *  names still open them. */
const sectionOf=kindAt;
const withCount=(b:HTMLButtonElement,n:number)=>{b.append(el('span',{class:'nav-count-muted','aria-hidden':'true'},String(n)));return b;};
/** What is in use, in the list's own unit (recurring sessions: its rows), as the setup guide counts: archived, inactive
 *  and people who left are not counted. */
const navCount=(kind:string)=>kind==='recurring_series'?seriesGroups(ctx(),toLocal(new Date().toISOString(),zone()).slice(0,10)).length:rows(kind).filter(r=>r.active!==false).length;
/** After a record is switched on or off in place, only the section counts change. */
const recount=async()=>{document.querySelectorAll<HTMLElement>('.config-nav [data-section]').forEach(b=>{const n=b.querySelector('.nav-count-muted');if(n&&b.dataset.section!=='setup')n.textContent=String(navCount(b.dataset.section!));});};
async function renderCatalogue(target:HTMLElement,kinds:string[]){
  // Every section has its own address (#configuration/department, #configuration/setup), so a link, a reload or Back
  // opens that section; a bare #configuration opens the last section shown and writes its address.
  const page=view,asked=routeParts[0]||'';
  if(routeQuery.has('open')&&(!routeQuery.get('open')||routeParts.length!==1||!catalogueSources().some(([p,list])=>p===page&&list.includes(sectionOf(asked))))){
    target.append(note('This record is not available. Choose one from the list.','warning'));return;
  }
  if(kinds.includes(sectionOf(asked)))catalogueKind=sectionOf(asked);
  else if(asked&&page==='configuration')tabRefused(['setup',...ALL_CONFIG_KINDS].includes(sectionOf(asked)),'');
  if(!kinds.includes(catalogueKind))catalogueKind=kinds.find(k=>k!=='setup'&&!isSettings(k))||kinds[0];
  const address=addressOf(catalogueKind);
  if(asked!==address){routeParts=[address];history.replaceState(null,'',routeHash(page,routeParts,routeQuery));}
  if(page==='configuration'&&catalogueKind!=='setup')configPicked=true;  // a section was picked: a bare #configuration no longer opens the guide
  const setup=catalogueKind==='setup',shown=catalogueKind;
  // The section left stays on screen (inert) while this one reads its data (renderPage): its nav marks the section
  // chosen at once, and a read that takes longer than 300 ms shows the pane busy (C-21).
  const before=document.querySelector<HTMLElement>('#main .config-layout');
  if(before&&!target.contains(before)){
    before.querySelectorAll<HTMLElement>('.config-nav [data-section]').forEach(b=>{const on=b.dataset.section===shown;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    const old=before.querySelector<HTMLElement>('.config-pane');
    if(old&&old.dataset.kind!==shown){old.setAttribute('aria-busy','true');delayed(()=>{if(!old.isConnected)return;const sk=skeleton('panel',`Loading ${kindLabel(shown)}…`);sk.classList.remove('sk-wait');old.replaceChildren(sk);});}  // the wait has passed: shown at once
  }
  const pane=el('div',{class:'config-pane','data-kind':shown});
  // The current section's button opens it afresh (its data read again), never a dead click.
  const navButton=(kind:string)=>{const health=kind==='setup'&&setupComplete(),b=button(health?'Data health':kindLabel(kind),()=>navigate(page+'/'+addressOf(kind)),'secondary',{'aria-current':shown===kind?'page':undefined,class:'button'+(shown===kind?' active':''),'data-section':kind});
    if(kind==='setup'){
      const steps=requiredSteps(),done=steps.filter(stepDone).length;
      if(!health){b.append(el('span',{class:'nav-count-muted','aria-hidden':'true'},`${done}/${steps.length}`));return b;}
      // Setup done: the guide is Data health, with what is left to fix in the attention tone.
      const fixes=dataHealth(ctx(),setupOpeners(),rankCodeClashes).length;
      if(fixes){b.append(el('span',{class:'nav-count','aria-hidden':'true'},String(fixes)));b.setAttribute('aria-label',`Data health, ${plural(fixes,'thing')} to fix`);}
      return b;}
    return isSettings(kind)||kind==='board'?b:withCount(b,navCount(kind));};
  const grouped=kinds.every(k=>CONFIG_GROUPS.some(([,list])=>list.includes(k)));
  const nav=el('nav',{class:'tabs config-nav','aria-label':grouped?'Configuration sections':'Sections'},...(grouped?CONFIG_GROUPS.filter(([,list])=>list.some(k=>kinds.includes(k))).map(([label,list])=>el('div',{class:'config-group'},el('span',{class:'config-group-label'},label),...list.filter(k=>kinds.includes(k)).map(navButton))):kinds.map(navButton)));
  target.append(el('div',{class:'config-layout'},nav,pane));
  // Phones show the sections as one sideways strip: bring the current one into view.
  requestAnimationFrame(()=>{const a=nav.querySelector<HTMLElement>('.active');if(a&&nav.scrollWidth>nav.clientWidth)nav.scrollLeft=a.offsetLeft-(nav.clientWidth-a.offsetWidth)/2;});
  target=pane;
  // A section that fails to read says so inside its pane: the nav stays usable (C-22).
  try{await drawSection();}
  catch(e){if(!(e instanceof ApiError)||sessionChanged(e))throw e;
    const failed=readFailed(kindLabel(shown).toLowerCase(),e,retryRefresh);
    pane.replaceChildren(sectionHead(kindLabel(shown),''),failed);}
  async function drawSection(){
  if(setup){await setupGuide(target);return;}
  if(catalogueKind==='names'){namesPage(target);return;}
  if(catalogueKind==='board'){await rotationBoard(target,{ctx:ctx(),zone:zone(),mine:false});return;}
  if(catalogueKind==='tags'){tagsPage(target,ctx(),permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR));return;}
  if(catalogueKind==='department'){await departmentSettingsPage(target);return;}
  if(catalogueKind==='branding'){target.append(brandingPanel());return;}
  if(catalogueKind==='auto_schedule'){await autoScheduleSettingsPage(target);return;}
  if(catalogueKind==='scheduling_groups'){await schedulingGroupsPage(target,{me:me!,zone:zone(),today:toLocal(new Date().toISOString(),zone()).slice(0,10),fields:ruleConditionFields(ctx()),words:c=>conditionWords({conditions:c})});return;}
  if(catalogueKind==='ai_provider'){await aiProviderPage(target,zone());return;}
  if(catalogueKind==='notifications'){const mine=renderSerial;await notificationPolicyPage(target,me!.department.id,dirty=>{if(mine===renderSerial)leaveCheck=dirty;});return;}
  if(catalogueKind==='integrations'){const mine=renderSerial;await integrationsPage(target,zone(),dirty=>{if(mine===renderSerial)leaveCheck=dirty;});return;}
  if(catalogueKind==='sign_in'){const mine=renderSerial;await signInConfigPage(target,dirty=>{if(mine===renderSerial)leaveCheck=dirty;});return;}
  const items=rows(catalogueKind);
  // Programme leads read rotation types (department-wide) but only coordinators change them.
  const creates=catalogueKind!=='rotation_types'||catalogueEditable(catalogueKind);
  const create=createLabel(catalogueKind);
  // Configuration's sections carry no tagline: what a kind is for is said once, in its empty state (C-01). Programmes
  // keep theirs above a list (P findings, their lane), never twice above an empty state.
  // No tagline under a section's name (AGENTS §2): what a kind is for is contextual help beside it; what it changes in
  // scheduling (effectOf, e.g. calendar entries never create leave) stays visible. Empty states say both.
  const head=sectionHead(kindLabel(catalogueKind),items.length?effectOf(catalogueKind):'',creates&&items.length?button(create,()=>catalogueForm(ctx(),catalogueKind),'primary'):null,
    catalogueKind==='leave_types'&&items.length&&permits(CAPABILITY.ADMIN)?button('Leave allowances',()=>leaveAllowanceSettings(ctx()),'secondary'):null);
  const about=aboutOf(catalogueKind);
  if(items.length&&about){
    const heading=head.querySelector('h2')!,title=el('div',{class:'page-title'});
    heading.replaceWith(title);
    title.append(heading,helpButton('About '+kindLabel(catalogueKind),about,'field-help'));
  }
  target.append(head);
  // One empty state everywhere (C-03): what is missing, why it matters, and the one action that adds it.
  // A kind that needs another first (a recurring session needs a duty type) offers that step instead of a Create that
  // could never be saved (C-28).
  if(!items.length){const missing=(CONFIG_KINDS[catalogueKind]?.needs||[]).map(k=>missingSetup(ctx(),k)).find(Boolean);
    target.append(empty('No '+kindLabel(catalogueKind).toLowerCase()+' yet',purposeOf(catalogueKind),
    missing||(creates?withIcon(button(create,()=>catalogueForm(ctx(),catalogueKind),'primary'),'plus'):null),'grid'));return;}
  const titleFor=(row:Row)=>catalogueKind==='mentorships'?name('people',row.person_id)+' · with '+name('people',row.mentor_id)+(row.strength==='required'?' · required':''):catalogueKind==='programme_memberships'?name('people',row.person_id)+' · '+name('programmes',row.programme_id):catalogueKind==='rotations'?name('people',row.person_id)+' · '+name('cohorts',row.cohort_id):labelOf(row);
  const textOf=(n:Child)=>n instanceof Node?n.textContent||'':String(n??'');
  const kind=catalogueKind;
  const nameCell=(row:Row)=>{
    const colour=CONFIG_KINDS[kind]?.colour==='type'?typeColour(row.id):kind==='categories'?rankColour(row.id):CONFIG_KINDS[kind]?.colour?colourOf(row):'';
    const heading=kind==='mentorships'?el('h3',{},personSource(row.person_id),' · with ',personSource(row.mentor_id),row.strength==='required'?' · required':'')
      :kind==='programme_memberships'?el('h3',{},personSource(row.person_id),' · '+name('programmes',row.programme_id))
      :kind==='rotations'?el('h3',{},personSource(row.person_id),' · '+name('cohorts',row.cohort_id)):el('h3',{},titleFor(row));
    const box=el('span',{class:'ct-name'},heading,el('small',row.description?{'data-ctip':row.description}:{},kind==='leave_types'?String(row.short_code):kind==='date_sets'?entrySummary(row):kind==='contact_types'&&String(row.starts_on)<'1000'&&!row.ends_on?'':row.description||`${row.starts_on?'From '+formatDate(row.starts_on):'From creation'}${row.ends_on?' until '+formatDate(lastIncludedDate(row.ends_on)):kind==='mentorships'?' · no end':''}`));
    if(kind==='programme_calendar_entries'){
      // Each member once, when their memberships of this programme together cover the whole entry (as the server's
      // confirmed_members: consecutive rows join, a gap does not).
      // Only each membership's dates (required ISO dates in the contract) are kept per person.
      const byPerson=new Map<string,{starts_on:string;ends_on:string}[]>();
      for(const m of rows('programme_memberships'))if(m.programme_id===row.programme_id)
        byPerson.set(m.person_id,[...(byPerson.get(m.person_id)||[]),{starts_on:String(m.starts_on),ends_on:String(m.ends_on)}]);
      const linked=protectedTimeRule(ctx(),row.id);
      // The newest version may wait for approval while an older one is still in force.
      const inForce=linked&&(linked.status==='active'||rows('policies').some(p=>p.status==='active'&&p.definition?.calendar_entry_id===row.id));
      if(linked)box.append(el('p',{class:'tag-row'},badge(`Protected time · ${linked.status==='active'?'active':inForce?'active · update waiting for approval':'waiting for approval'}`,inForce?'ok':'warn'),el('span',{class:'muted'},labelOf(linked))));
      // Coverage as a badge; the section's visible line (effectOf) already says calendar entries never create leave.
      const covered=[...byPerson.values()].filter(list=>membershipCovers(list,row.starts_on,row.ends_on)).length;
      box.append(el('p',{class:'tag-row'},name('programmes',row.programme_id)+' · '+row.kind.replaceAll('_',' '),
        badge(covered?`${plural(covered,'member')} covered`:'No confirmed members cover this period',covered?'':'warn')));
    }
    if(kind==='rotations')box.append(row.rotation_type_id?el('p',{},name('rotation_types',row.rotation_type_id)):note('No rotation type selected; rotation-specific workload rules do not apply.','warning'),el('p',{class:'readonly-value'},(row.service_id?name('services',row.service_id):'Services defined by rotation rules')+(row.mentor_id?' · Mentor: '+name('people',row.mentor_id):'')),badge(row.department_available===false?'External rotation · unavailable':'Internal rotation · may assign eligible duties',row.department_available===false?'warn':''));
    return el('div',{class:'ct-main'},colour?el('span',{class:`ct-swatch duty-${colour}`,'aria-hidden':'true'},el('span',{})):null,box);
  };
  // SW3-D-07: the shared history (who, newest first). Restore needs a catalogue restore command (none yet).
  const showHistory=(row:Row)=>historyDialog('Definition history','Name',async()=>{const h=await get(`/api/catalogues/${kind}/${row.id}/history`);
    return {more:false,versions:[...h.versions].sort((a:Row,b:Row)=>b.object_revision-a.object_revision).map((r:Row)=>({version:r.object_revision,saved:formatDate(r.created_at,zone()),
      actor_id:r.actor_id,reason:r.reason,value:r.snapshot.name||r.snapshot.display_name||titleFor(row)}))};},titleFor(row));
  // Past its end date reads as ended, not active.
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10),statusOf=(r:Row)=>r.active===false?(kind==='leave_types'?'Archived':'Inactive'):activeOn(r,today)?'Active':r.starts_on>today?'Upcoming':'Ended';
  // Recurring sessions: one row per series type and the clinic week grid (recurring_sessions.ts).
  if(kind==='date_sets')await loadUpcoming(items);
  if(kind==='recurring_series'){recurringSessions(target,{ctx:ctx(),editable:catalogueEditable(kind),today,refresh,bulk:ids=>catalogueBulkActions(kind,ids),history:showHistory});return;}
  const list=dataTable<Row>({id:'catalogue-'+kind,noun:[singularOf(kind),kindLabel(kind).toLowerCase()],rows:items,rowId:r=>r.id,
    ...(registry(kind)?{collection:kind,afterChange:()=>refresh()}:{}),
    searchLabel:'Search '+kindLabel(kind).toLowerCase(),searchPlaceholder:'Search '+kindLabel(kind).toLowerCase(),
    columns:[
      {key:'name',label:singularOf(kind).replace(/^./,c=>c.toUpperCase()),text:r=>`${titleFor(r)} ${r.description||''}`,sort:r=>titleFor(r),cell:nameCell,
        edit:items.every(r=>typeof r.name==='string')?inlineField(kind,'name',{kind:'text',required:true,maxLength:160}):undefined},
      ...catalogueColumns(kind).map(([label,render],n)=>({key:'c'+n,label,cell:(r:Row)=>render(r),text:(r:Row)=>textOf(render(r)),facet:(r:Row)=>textOf(render(r))})),
      // Leave types: reordered and switched in place (the order is the Type picker's order).
      ...(kind==='leave_types'?[
        {key:'blocks',label:'Blocks duties',text:(r:Row)=>r.counts_as_unavailable===false?'No':'Yes',facet:(r:Row)=>r.counts_as_unavailable===false?'No':'Yes',cell:(r:Row)=>r.counts_as_unavailable===false?el('span',{class:'muted'},'No · shown only'):'Yes',
          edit:inlineField(kind,'counts_as_unavailable',{kind:'toggle',value:r=>r.counts_as_unavailable!==false})},
        {key:'order',label:'Order',num:true,sort:(r:Row)=>Number(r.sort_order),text:(r:Row)=>String(r.sort_order),cell:(r:Row)=>el('span',{class:'ct-mono'},String(r.sort_order)),
          edit:inlineField(kind,'sort_order',{kind:'number',required:true})}]:[]),
      ...(rows('tags').some(t=>(t.labels||[]).includes(kind))?[{key:'tags',label:'Tags',text:(r:Row)=>tagsFor(ctx(),kind,r.id).map(t=>t.name).join(' '),
        search:'related' as const,terms:(r:Row)=>tagsFor(ctx(),kind,r.id).map(t=>t.name),refs:{kind:'tags' as const,ids:(r:Row)=>tagsFor(ctx(),kind,r.id).map(t=>t.id)},
        facet:(r:Row)=>tagsFor(ctx(),kind,r.id).map(t=>'#'+t.name),cell:(r:Row)=>tagRefs(ctx(),kind,r.id,openTag)}]:[]),
      {key:'status',label:'Status',text:statusOf,facet:statusOf,cell:r=>el('span',{class:'ct-status '+(statusOf(r)==='Active'?'on':'off'),...(statusOf(r)==='Archived'?{'data-tip':'Stays on old leave; cannot be chosen'}:{})},statusOf(r)),
        edit:items.every(r=>typeof r.active==='boolean')?inlineField(kind,'active',{kind:'toggle',value:r=>r.active!==false}):undefined},
    ],
    bulk:catalogueEditable(kind)?ids=>rows(kind).filter(r=>ids.includes(r.id)).every(rowEditable(kind))?catalogueBulkActions(kind,ids):[]:undefined,
    rowButtons:r=>[...(kind==='rotation_types'&&may('rule.propose')?[button('Add rotation rule',()=>policyForm(ctx(),r.id),'secondary small')]:[]),
      ...(kind==='cohorts'&&may('rule.propose')?[button('Add cohort rule',()=>policyForm(ctx(),'',{cohort_id:r.id,title:labelOf(r)+' · '}),'secondary small')]:[]),
      ...(kind==='programme_calendar_entries'?[button('Protected time',()=>calendarProtectionPanel(ctx(),r),'secondary small')]:[]),
      ...(kind==='date_sets'?[button('Dates',()=>dateSetDates(ctx(),r),'secondary small',{'aria-label':'Dates of '+labelOf(r)})]:[]),
      ...(rowEditable(kind)(r)?[button('Edit',()=>catalogueForm(ctx(),kind,r),'secondary small')]:[])],
    rowActions:r=>[{label:'History',icon:'history',run:()=>showHistory(r)},
      ...(kind==='date_sets'&&catalogueEditable(kind)&&r.active!==false?[{label:'Import entries…',icon:'upload' as const,run:()=>importDateSet(ctx(),r)},{label:'Copy to next year…',icon:'copy' as const,run:()=>copyDateSet(ctx(),r)}]:[])],
  });
  target.append(list.root);
}}
/** Who may change this catalogue: its edit command's roles from the Bulk registry (editRoles, the server's command_roles),
 *  so single edit, Bulk and Paste agree. Object gate on top (the server's programme_access, services/catalogue.py): a
 *  programme lead changes only records in a programme they lead, and a rotation type belongs to none. */
const catalogueEditable=(kind:string)=>{const roles=editRoles(kind);
  return (roles?permits(...roles):may('catalogue.update'))&&!(kind==='rotation_types'&&!permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR));};
/** A programme lead (not a coordinator) reads shared rotation templates (no programme) but changes only their own
 *  programmes' ones; inline cells, which edit any row, stay with coordinators there. */
const leadTemplates=(kind:string)=>kind==='rotation_templates'&&!permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR);
/** The server's programme_write_access: a programme lead (not a coordinator) changes a membership only in a programme
 *  they lead (they own it, or hold a current membership in it), and never their own membership. */
function programmeMembershipWritable(r:Row){
  if(permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR))return true;
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10);
  return !!me?.person_id&&r.person_id!==me.person_id&&(find('programmes',r.programme_id)?.owner_identity_id===me.identity_id
    ||rows('programme_memberships').some(m=>m.person_id===me!.person_id&&m.programme_id===r.programme_id&&m.starts_on<=today&&m.ends_on>today));
}
const rowEditable=(kind:string)=>(r:Row)=>catalogueEditable(kind)&&!(leadTemplates(kind)&&!r.programme_id);
/** Inline editing for a configuration field: saves one or many rows with one checked, audited command. */
function inlineField(kind:string,fieldName:string,edit:Omit<EditSpec<Row>,'save'|'value'>&{value?:(r:Row)=>unknown},allowed=catalogueEditable(kind)&&!leadTemplates(kind)):EditSpec<Row>|undefined{
  if(!allowed)return undefined;
  // The reply's records patch the list and the lists in memory in place (C-34); switching one on or off recounts the nav.
  return {...edit,value:edit.value||(r=>r[fieldName]),...(fieldName==='active'?{afterSave:recount}:{}),save:async(list,value)=>{
    const result=await command('catalogue.bulk_update',{catalogue:kind,items:list.map(r=>({id:r.id,revision:r.revision})),set:{[fieldName]:fieldName==='short_code'?String(value).toUpperCase():value},reason:'Inline edit'});
    // Patch the saved fields while retaining the derived facts from the list read.
    const byId=new Map((result.records as Row[]).map(r=>[r.id,r]));
    const merge=(r:Row)=>{
      const saved=byId.get(r.id);
      if(!saved)return r;
      if(kind!=='people')return {...r,...saved};
      const {effective_active,...person}=saved;
      // Older receipts carry only the configured flag. Preserve the list status when that flag did not change.
      const active=effective_active??(person.active===(r.configured_active??r.active)?r.active:person.active);
      return {...r,...person,configured_active:person.active,active};
    };
    catalogues={...catalogues,[kind]:rows(kind).map(merge)};setTypeColours(rows('assignment_types'));setRankColours(rows('categories'));return list.map(merge);}};
}
const revisionsOf=(kind:string,ids:string[])=>rows(kind).filter(r=>ids.includes(r.id)).map(r=>({id:r.id,revision:r.revision}));
/** One reason, one checked command for every selected record. */
function bulkUpdate(kind:string,ids:string[],change:Record<string,unknown>,title:string,done:()=>void,extra?:(form:HTMLFormElement)=>()=>Record<string,unknown>){
  formDialog(title,`${ids.length} record${ids.length===1?'':'s'} change together or not at all. Each keeps its own history.`,form=>{
    const more=extra?.(form);const reason=area('Reason for this change','',2);reason.input.placeholder='Optional note';form.append(reason.wrap);
    return async()=>{const result=await command('catalogue.bulk_update',{catalogue:kind,items:revisionsOf(kind,ids),...change,...(more?.()||{}),reason:routineReason(reason.input.value)});notify(result.message);done();await refresh();};
  },'Apply to '+ids.length);
}
const colourPicker=(form:HTMLFormElement)=>{const c=colourSwatches('',true,'Automatic gives each record its own stable colour.');form.append(c.wrap);return ()=>({colour:c.input.value});};
function switchOff(kind:string,ids:string[],verb:string,done:()=>void){
  return runBulk({collection:kind,noun:[singularOf(kind),kindLabel(kind)?.toLowerCase()||kind],destructive:true,verb,table:kind==='people'?'people':'catalogue-'+kind,onDone:async()=>{done();await refresh();},
    body:{item_kind:'catalogue.update',common:{catalogue:kind},items:revisionsOf(kind,ids).map(r=>({target_id:r.id,expected_revision:r.revision})),changes:[{field:'active',how:'set',value:false}],reason:`${verb} in bulk`}});
}
function catalogueBulkActions(kind:string,ids:string[]):BulkAction[]{
  const picked=rows(kind).filter(r=>ids.includes(r.id)),colour=CONFIG_KINDS[kind]?.colour,coloured=colour==='type'||colour==='own',[off,on]=kind==='leave_types'?['Archive','Restore']:['Switch off','Switch on'];
  // Dated relationships have no active field; only offer switches supported by the returned record contract.
  const switchable=picked.length>0&&picked.every(r=>typeof r.active==='boolean');
  return [
    // Switching off goes through Preview: Apply there is the armed confirm (never armed straight from the bar).
    ...(switchable&&picked.some(r=>r.active!==false)?[{label:off,icon:'power' as const,danger:true,run:(i:string[],d:()=>void)=>switchOff(kind,i,off,d)}]:[]),
    ...(switchable&&picked.some(r=>r.active===false)?[{label:on,icon:'check' as const,run:(i:string[],d:()=>void)=>bulkUpdate(kind,i,{active:true},on+' '+i.length+' records',d)}]:[]),
    ...(coloured?[{label:'Set colour',icon:'star' as const,run:(i:string[],d:()=>void)=>bulkUpdate(kind,i,{},'Set colour for '+i.length+' records',d,colourPicker)}]:[]),
  ];
}
function peopleBulkActions(ids:string[]):BulkAction[]{
  // One engine (P-07): every change goes through Change… (the registry's panel: Preview, Results, one Undo). The bar
  // keeps two shortcuts that open it with their change chosen; everything else is in Change…'s list.
  const picked=rows('people').filter(p=>ids.includes(p.id)),manage=permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR);
  const change=(preset:BulkChange[])=>(chosen:string[],done:()=>void,all?:SelectionValue)=>openChange({collection:'people',noun:['person','people'],...(all?{selection:all}:{ids:chosen}),
    revision:id=>rows('people').find(p=>p.id===id)?.revision,table:'people',labelOf:id=>fullNameOf(rows('people').find(p=>p.id===id)),preset,onDone:async()=>{done();await refresh();}});
  const actions:BulkAction[]=[
    ...(manage?[{label:'Add tag',icon:'plus' as const,run:change([{field:'tags',how:'add'}])},{label:'Give rank',icon:'people' as const,run:change([{item_kind:'membership.create'}])},
      {label:'Set a rule number…',icon:'rules' as const,more:true,run:(i:string[],d:()=>void)=>setRuleNumber(picked.filter(p=>i.includes(p.id)),async()=>{d();await refresh();})}]:[]),
    ...(permits(CAPABILITY.CREDENTIALER)&&!manage?[{label:'Authorise clinics',icon:'shield' as const,run:change([{item_kind:'eligibility.create'}])},{label:'Grant skills',icon:'check' as const,run:change([{item_kind:'grant.create'}])}]:[]),
    // The one Record leave form (forms.ts availabilityFields: Leave, Day off, Actual absence, External, Protected, All
    // day), with the selected people already chosen; several people go through one bulk change with one Undo.
    ...(permits(CAPABILITY.COORDINATOR)?[{label:'Record leave',icon:'leave' as const,more:true,run:(i:string[],d:()=>void)=>formDialog(`Record leave for ${plural(i.length,'person','people')}`,'',form=>{
      const save=availabilityFields(ctx(),form,false,{person_ids:i});return async()=>{await save();d();};},'Record leave')}]:[]),
    ...(picked.some(p=>(p.configured_active??p.active)!==false)&&manage?[{label:'Switch off',icon:'power' as const,danger:true,more:true,run:(i:string[],d:()=>void)=>switchOff('people',i,'Switch off',d)}]:[]),
    ...(picked.length&&picked.every(p=>p.active===false)&&permits(CAPABILITY.ADMIN)?[{label:'Remove personal details',icon:'trash' as const,danger:true,run:(i:string[],d:()=>void)=>confirmAction(`Remove personal details of ${plural(i.length,'former staff member')}`,'For people who have left. Their name, contact and staff details, private leave reasons and notes, calendar links and sign-in are removed for good; they appear as “Former staff member”. Rotas, publications and the audit log keep what they recorded.',async reason=>{await runBulk({collection:'people',noun:['person','people'],destructive:true,verb:'Remove details of',message:'Personal details removed for {n} people.',onDone:async()=>{d();await refresh();},
      body:{item_kind:'person.anonymise',items:i.map(id=>({target_id:id,expected_revision:picked.find(p=>p.id===id)!.revision})),reason}});},'Remove details')}]:[]),
  ];
  return actions;
}
// One rule, many lines: copies that differ only by who, when, which days or the number (imported per-person,
// per-week or per-weekday copies) read as one rule. The name without people or dates joins them.
function ruleStemFor(p:Row,people:Map<string,string>){
  const sc=p.definition?.scope||{};let n=String(p.name);
  for(const id of [sc.person_id,...(sc.person_ids||[])].filter(Boolean)){const who=people.get(id);if(who)n=n.split(who).join('');}
  const parts=n.replace(/\d{4}-\d{2}(-\d{2})?/g,'').split(/\s+[·\-–]\s+|^\s*[·\-–]\s*|\s*[·\-–]\s*$/).map(x=>x.trim()).filter(Boolean);
  const out=parts.join(' · ');return out?out[0].toUpperCase()+out.slice(1):String(p.name);}
const ruleFamilyFor=(p:Row,people:Map<string,string>)=>`${p.definition.rule.kind}|${ruleStemFor(p,people).toLocaleLowerCase()}`;
/** A work phone as a dial link; several office extensions stay separate links (never one long number). */
const dial=(value:unknown)=>{const v=String(value||'').trim();return v?el('a',{href:'tel:'+v.replace(/[^+\d]/g,'')},v):el('span',{class:'muted'},'—');};
const extensions=(value:unknown)=>{const parts=String(value||'').split(/\s*[-,;/]\s*/).filter(Boolean);return parts.length?el('span',{},...parts.map((n,i)=>el('span',{},i?' / ':'',dial(n)))):dial('');};
/** People = the department directory and, for those who manage them, ranks, eligibility and skills: one list whose
 * columns and actions follow the role. Staff read contacts from /api/directory (they have no catalogues). */
async function peoplePage(target:HTMLElement){
  // The list's As of day ('' = today), as its shared View holds it: ranks, programmes, clinics and skills are read on
  // that day, as the server's resolver reads them for Select by condition and Show their… (what you see is what gets selected).
  const peopleDay=tableAsOf('people');
  const full=!peopleDirectory(),manager=permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR),cred=permits(CAPABILITY.CREDENTIALER);
  const people:Row[]=full?rows('people'):(directoryRows=(await get<{people:Row[]}>('/api/directory')).people);
  // One primary action: New person for whoever manages people, Authorise eligibility for a credentialer.
  // Ranks and skills for one person are row actions (several people: bulk); the programme roster acts on a whole programme, so it stays here.
  // On a phone People is the read-only Where is everyone: no desktop forms in its head.
  const desk=!phoneLite();
  target.append(sectionHead('People','',manager&&desk?button('New person',()=>newPerson(),'primary'):null,
    cred&&!manager&&desk?button('Authorise eligibility',()=>relationshipForm(ctx(),'eligibility'),'primary'):null,cred&&desk?button('Authorise programme',()=>programmeEligibilityForm(ctx()),'secondary'):null));
  const on=peopleDay||toLocal(new Date().toISOString(),zone()).slice(0,10),current=(r:Row)=>!r.revoked&&r.active!==false&&r.starts_on<=on&&(!r.ends_on||r.ends_on>on);
  // Everything a row needs, worked out once per person: each record list is grouped by person in one pass.
  const byPerson=(kind:string)=>{const m=new Map<string,Row[]>();for(const r of rows(kind)){const l=m.get(r.person_id);if(l)l.push(r);else m.set(r.person_id,[r]);}return m;};
  const eligibleOf=byPerson('eligibilities'),grantsOf=byPerson('grants'),ranksOf=byPerson('memberships'),programmesOf=byPerson('programme_memberships');
  const info=new Map(people.map(p=>{
    // Current rank and programmes are the server's (packages/domain/people.py: the department's local day, the same
    // dated facts as rules), so Home's "without a rank" count and this column agree.
    // Another day: the dated memberships themselves, on that day.
    const ranks:string[]=peopleDay?(ranksOf.get(p.id)||[]).filter(current).map(r=>String(r.category_id)):p.current_category_ids||[];
    const programmes:string[]=peopleDay?(programmesOf.get(p.id)||[]).filter(current).map(r=>String(r.programme_id)):p.current_programme_ids||[];
    const services=[...new Set((eligibleOf.get(p.id)||[]).filter(current).map(r=>String(r.service_id)))];
    const skills=(grantsOf.get(p.id)||[]).filter(current).map(r=>String(r.capability_id));
    // What blocks scheduling (warning) or is missing from the record (neutral): the "Details to review" filter.
    const gaps:[string,string,Tone|''][]=[];
    if(full&&p.active!==false&&!ranks.length)gaps.push(['No rank','No current rank or department membership','warn']);
    if(full&&p.active!==false&&!services.length)gaps.push(['No clinic eligibility','Required before assigning clinical duties','warn']);
    // A withheld number (employee_number_redacted) is not a missing one.
    if(full&&p.employee_number_redacted===false&&!p.staff_number)gaps.push(['No employee number','Employee number not provided','']);
    return [p.id,{ranks,services,skills,programmes,gaps}];
  }));
  const of=(p:Row)=>info.get(p.id)!;
  // Rows stay one line (pillList): the first chips, then "+N" with the whole list; Wrap this column shows them all.
  // Chips keep the list's own order (Configuration), the same on every row.
  const chipsOf=(kind:string,ids:string[],label:MoreNoun,cls='role-chip')=>{const at=new Map(rows(kind).map((r,i)=>[r.id,i]));
    const unique=[...new Set(ids)].sort((a,b)=>(at.get(a)??1e9)-(at.get(b)??1e9));
    // A rank in its own tint (C21; the chip names it); skills carry no colour, so they stay neutral; clinics keep their hue.
    return pillList(unique.map(id=>el('span',{class:`${cls} duty-${kind==='categories'?rankColour(id):kind==='capabilities'?'slate':colourOf(find(kind,id),id)}`,...(kind==='categories'?{'data-lg-self':''}:{})},bidi(name(kind,id)))),{label});};
  const none=()=>el('span',{class:'muted'},'—');
  // A person carries a tag by membership or (older records) by its name: either finds a picked tag.
  const tagIds=new Map(rows('tags').map(t=>[String(t.name).toLowerCase(),t.id as string]));
  const tagIdsOf=(p:Row)=>[...tagsFor(ctx(),'people',p.id).map(t=>t.id as string),...((p.tags||[]) as string[]).map(t=>tagIds.get(t.toLowerCase())||'')];
  const records:Column<Row>[]=!full?[]:[
    // Rank groups read in the department's rank order (Configuration › Ranks), not A–Z.
    // A person with two current ranks is grouped once, under the first in that order (groupFirst); Filter matches either.
    {key:'rank',label:'Current rank',ordered:rows('categories').map(r=>name('categories',r.id)),groupFirst:true,text:p=>of(p).ranks.map(id=>name('categories',id)).join(' '),terms:p=>of(p).ranks.map(id=>name('categories',id)),facet:p=>of(p).ranks.length?of(p).ranks.map(id=>name('categories',id)):['No current rank'],ids:p=>of(p).ranks,
      cell:p=>of(p).ranks.length?chipsOf('categories',of(p).ranks,['rank','ranks']):badge('No current rank','warn')},  // a scheduling gap (P-25)
    {key:'services',label:'Clinical eligibility',text:p=>of(p).services.map(id=>name('services',id)).join(' '),terms:p=>of(p).services.map(id=>name('services',id)),facet:p=>of(p).services.length?of(p).services.map(id=>name('services',id)):['No current eligibility'],ids:p=>of(p).services,sort:p=>of(p).services.length,
      cell:p=>of(p).services.length?chipsOf('services',of(p).services,['clinic','clinics']):badge('No current eligibility','warn')},
    // Search reads skills, programmes and tags as related records (a picked one by id), each name on its own.
    {key:'skills',label:'Skills',text:p=>of(p).skills.map(id=>name('capabilities',id)).join(' '),search:'related',terms:p=>of(p).skills.map(id=>name('capabilities',id)),refs:{kind:'capabilities',ids:p=>of(p).skills},facet:p=>of(p).skills.map(id=>name('capabilities',id)),sort:p=>of(p).skills.length,
      cell:p=>of(p).skills.length?chipsOf('capabilities',of(p).skills,['skill','skills'],'tag-chip'):none()},
    {key:'programme',label:'Programme',text:p=>of(p).programmes.map(id=>name('programmes',id)).join(' '),search:'related',terms:p=>of(p).programmes.map(id=>name('programmes',id)),refs:{kind:'programmes',ids:p=>of(p).programmes},facet:p=>of(p).programmes.map(id=>name('programmes',id)),
      cell:p=>of(p).programmes.length?chipsOf('programmes',of(p).programmes,['programme','programmes'],'tag-chip'):none()},
    {key:'tags',label:'Tags',text:p=>(p.tags||[]).join(' '),search:'related',terms:p=>(p.tags||[]) as string[],refs:{kind:'tags',ids:p=>tagIdsOf(p)},facet:p=>(p.tags||[]) as string[],
      cell:p=>{const names=(p.tags||[]) as string[],named=new Set(names.map(n=>n.toLowerCase())),linked=tagsFor(ctx(),'people',p.id);
        // A confirmed inline edit can precede refreshed membership metadata; show its names without stale counts.
        return linked.length&&linked.length===named.size&&linked.every(t=>typeof t.name==='string'&&named.has(t.name.toLowerCase()))?tagRefs(ctx(),'people',p.id,openTag):names.length?pillList(names.map(t=>el('span',{class:'tag-chip'},t)),{label:['tag','tags']}):none();},
      // The reply carries the person, not the tag lists it moved (members, a new tag): read them, as its pulse no longer does.
      edit:inlineField('people','tags',{kind:'multi',creatable:true,value:p=>p.tags||[],options:()=>[...new Set(rows('people').flatMap(x=>(x.tags||[]) as string[]))].sort().map(t=>({value:t,label:t})),
        afterSave:()=>refresh(true,new Set<LiveScope>(['catalogue','people']))})},
    {key:'code',label:'Short code',field:'short_code',text:p=>shortCode(p),cell:p=>el('span',{class:'mono'},p.short_code||el('span',{class:'muted'},'Automatic')),
      edit:inlineField('people','short_code',{kind:'text',maxLength:6})},
  ];
  // Build optional columns and search terms only from contacts already disclosed to this reader.
  const contactTypes=new Map<string,VisibleContact>();
  const contactValues=new Map(people.map(p=>{
    const values=new Map<string,string>();
    for(const c of (p.contacts||[]) as VisibleContact[]){
      if(c.legacy_field==='work_phone'||c.legacy_field==='work_extension')continue;
      contactTypes.set(c.contact_type_id,c);
      values.set(c.contact_type_id,c.value);
    }
    return [p.id,values] as const;
  }));
  const extraContacts:Column<Row>[]=[...contactTypes.values()]
    .sort((a,b)=>a.sort_order-b.sort_order||a.name.localeCompare(b.name)||a.contact_type_id.localeCompare(b.contact_type_id))
    .map(c=>{
      const text=(p:Row)=>contactValues.get(p.id)?.get(c.contact_type_id)||'';
      return {key:'contact_'+c.contact_type_id,label:c.name,text,cell:p=>text(p)||none()};
    });
  const contact:Column<Row>[]=[
    // The title shown: the recorded one, else the current rank (display_title; the directory sends it as title).
    // The recorded title only: the rank is its own column (P-02).
    {key:'title',label:'Title',text:p=>String(p.title??''),facet:p=>String(p.title??''),cell:p=>p.title||none()},
    {key:'section',label:'Section',field:'directory_section',text:sectionLabel,facet:sectionLabel,cell:p=>sectionLabel(p)||none()},
    {key:'phone',label:'Work phone',field:'work_phone',text:p=>String(p.work_phone||''),cell:p=>dial(p.work_phone)},
    {key:'ext',label:'Extension',field:'work_extension',text:p=>String(p.work_extension||''),cell:p=>extensions(p.work_extension)},
    ...extraContacts,
  ];
  // Columns follow the role: contacts for everyone; ranks, eligibility and skills for planners; what a credentialer authorises.
  const hidden=!full?[]:manager?['title','section','programme','code','status','review']:cred?['title','section','rank','programme','tags','code','status','review']:['title','section','skills','programme','code','status'];  // tags show: their chips open the tag read-only
  // People › Timeline (View settings › Layout): planners' lists, once the server has the person timeline. Only roles the
  // server lets read it (GET /api/timeline: staff and team readers) ask, so a credentialer's People never draws a 403.
  // areasChanged waits a microtask: a rebuilt page draws the last answer while `table` below is still being built.
  const timeline=full&&(may('schedule.read_team')||permits(CAPABILITY.STAFF))&&await timelineAvailable()?peopleTimeline<Row>({open:p=>personDetails(p),areasChanged:()=>queueMicrotask(()=>{if(table.uses('works_in_area'))table.refresh();table.fieldsChanged();}),leaveTypes:()=>rows('leave_types').map(r=>[String(r.id),String(r.name)] as [string,string])}):undefined;
  // SW3-D-25: rows are named by the person's name; the name column's text also holds every alias (for search).
  const table=dataTable<Row>({id:'people',noun:['person','people'],rows:people,rowId:p=>p.id,rowLabel:p=>String(p.display_name||''),...(full?{collection:'people',afterChange:()=>refresh(),asOf:()=>{if(tableAsOf('people')!==peopleDay)void renderPage();}}:{}),
    // Where is everyone (approved v2, D48): everyone's one line over 12 months, by rank.
    ...(timeline?{timeline,pinned:phoneLite()?'Where is everyone':undefined,presets:[{label:'Where is everyone',definition:{layout:'timeline',group:'rank',filters:{status:['Active']},
      timeline:{one_line:true,zoom:'year',bar_label:'short',hidden:['short'],density:'comfortable'}}}]}:{}),
    searchLabel:'Search people',searchKey:compactSearch,searchPlaceholder:full?'Name, code, rank, clinic, phone or tag':'Name, title, phone or extension',
    empty:manager?['No people yet','Add people, then give them a rank and clinical eligibility so they can be scheduled.']:['No people yet','Contacts appear here once people are added.'],
    columns:[
      {key:'name',label:'Person',text:p=>[p.display_name,...(p.aliases||[]),full?shortCode(p):'',...(p.tags||[])].join(' '),terms:p=>[p.display_name,...(p.aliases||[]),full?shortCode(p):''],sort:p=>String(p.display_name),
        cell:p=>el('div',{class:'person-cell'},el('div',{},full?el('span',{class:`code-chip duty-${colourOf(p)}`,'aria-label':'Short code '+shortCode(p)},shortCode(p)):null,el('strong',{},nameText(p.id,String(p.display_name)))),p.active===false?el('div',{class:'tag-row'},statusBadge('inactive','Inactive')):null)},
      ...records,
      // Works in: the areas worked over the line's window (the main one first); Group puts a person under the main one.
      ...(timeline?[{key:'works_in_area',label:'Works in',groupFirst:true,text:(p:Row)=>timeline.worksIn(p.id).join(' '),terms:(p:Row)=>timeline.worksIn(p.id),facet:(p:Row)=>timeline.worksIn(p.id),
        cell:(p:Row)=>{const a=timeline.worksIn(p.id);return a.length?pillList(a.map(x=>el('span',{class:'tag-chip'},x)),{label:['area','areas']}):none();}}]:[]),
      ...contact,
      ...(!full?[]:[{key:'status',label:'Status',field:'active',options:['Active','Inactive'],text:(p:Row)=>p.active===false?'Inactive':'Active',facet:(p:Row)=>p.active===false?'Inactive':'Active',
        cell:(p:Row)=>p.active===false?statusBadge('inactive','Inactive'):statusBadge('active','Active'),  // the one status palette (P-21)
        edit:inlineField('people','active',{kind:'toggle',value:p=>(p.configured_active??p.active)!==false})}]),
      // Details to review is for the people who can fix them (editors; credentialers for clinic eligibility).
      ...(manager||cred?[{key:'review',label:'Details to review',options:['Needs attention','Complete'],facet:(p:Row)=>of(p).gaps.length?'Needs attention':'Complete',sort:(p:Row)=>of(p).gaps.length,
        cell:(p:Row)=>of(p).gaps.length?pillList(of(p).gaps.map(([text,tip,tone])=>{const b=badge(text,tone);b.setAttribute('data-tip',tip);return b;}),{label:['detail to review','details to review']}):el('span',{class:'muted'},'Complete')}]:[]),
      {key:'order',label:'Directory order',hidden:true,cell:()=>null,sort:p=>Number(p.directory_order??1000)},
    ],
    // Active people by default (leavers stay one filter away); the directory keeps its own order.
    defaults:{hidden:[...hidden,...extraContacts.map(c=>c.key),...(timeline?['works_in_area']:[])],...(full?{filters:{status:['Active']}}:{sort:['order','asc']})},
    bulk:permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.CREDENTIALER)?ids=>peopleBulkActions(ids):undefined,
    // The row opens the person's side panel; one visible button per row.
    onOpen:(p,position)=>personDetails(p,position),
    // One person editor: the side panel reviewed; its sections open the forms.
    rowButtons:p=>manager?[button('Open',()=>personDetails(p),'secondary small',{'aria-label':'Open '+p.display_name})]:[],
    // QA-043b: the row (and its Open button) opens the panel, so the ⋯ menu offers where to go in it (the panel's own
    // sections for this reader, personSections) and what this reader may do, by the contract's permissions.
    rowActions:p=>{if(!full)return [];
      const open=personSections(ctx(),p,holdsLists(catalogues)&&canLookupRules()).map(([key,label,icon])=>({label,icon,run:():void=>personDetails(p,table.position(p.id),key)}));
      const acts=[...(may('availability.record')?[{label:'Record leave',icon:'leave' as const,run:()=>availabilityForm(ctx(),false,{person_id:p.id})}]:[]),
        ...(may('membership.create')?[{label:'Add rank',icon:'people' as const,run:()=>relationshipForm(ctx(),'membership',p)}]:[]),
        ...(may('eligibility.create')?[{label:'Authorise eligibility',icon:'shield' as const,run:()=>relationshipForm(ctx(),'eligibility',p)}]:[]),
        ...(may('grant.create')?[{label:'Grant skill',icon:'check' as const,run:()=>relationshipForm(ctx(),'grant',p)}]:[]),
        ...(may('rule.propose')?[{label:'Add a rule for this person',icon:'rules' as const,run:()=>policyForm(ctx(),'',{person_id:p.id,title:p.display_name+' · '})}]:[])];
      return [...(open.length?[{group:'Open',items:open}]:[]),...(open.length&&acts.length?['separator' as const]:[]),...acts];},
    });
  target.append(el('section',{class:'panel'},table.root));
  // Resolve links only within this account's projection. A stale link must not open a different matching name.
  const open=routeQuery.get('open'),person=open?people.find(p=>p.id===open):undefined;
  // DL-027: a shared link to someone this reader is not shown lands on the list with a note true either way, never
  // a blank or another person. (A link to someone since removed reads the same: the projection cannot tell them apart.)
  if(open&&!person)target.prepend(note(TERMS.personUnavailable,'info'));
  if(person&&personPanel?.id!==person.id)personDetails(person,table.position(person.id),routeQuery.get('tab')||'overview');  // "N of M" with ↑ ↓ (P-09)
  else if(!open&&personPanel&&document.querySelector('.dock-panel.person-panel'))dockLeave();  // Back from #people?open=: the card closes (asking first if it holds changes)
  else personPanel?.redraw();
}
/** Which rules apply to one person on a day (Rules › Which rule applies?, and the person's side panel). */
function whichRuleLookup(personId='',pickPerson=true):HTMLElement{
  const who=select('Person',peopleChoices(ctx()),personId),day=field('Day','date',toLocal(new Date().toISOString(),zone()).slice(0,10),{required:true});
  const out=el('div',{'aria-live':'polite'});
  let request=0;
  const run=async()=>{
    const current=++request;
    if(!who.input.value||!day.input.value){out.replaceChildren();return;}
    out.replaceChildren(working('Finding applicable rules…'));
    try{
      const result=await get<{rules:Row[]}>(`/api/policies/applies?person_id=${encodeURIComponent(who.input.value)}&day=${day.input.value}`);
      if(current!==request||!out.isConnected)return;
      // Rules that ask the same thing for the same reason read as one row with a count (a department can have hundreds).
      const same=new Map<string,{names:string[];r:Row}>();
      for(const r of result.rules){const k=String(JSON.stringify([r.applies,ruleSentence(r.rule),r.why]));const g=same.get(k);if(g)g.names.push(String(r.name));else same.set(k,{names:[String(r.name)],r});}
      const applying=result.rules.filter(r=>r.applies).length;
      // One row per rule (P-06): its name and sentence, the numbers, a badge only when a more specific line replaces it.
      out.replaceChildren(...(result.rules.length?[el('p',{class:'muted'},`${plural(applying,'rule applies','rules apply')}${result.rules.length>applying?` · ${result.rules.length-applying} replaced`:''}.`),
        el('ul',{class:'ph-rows which-rule-rows'},...[...same.values()].map(({names,r})=>profileRow(el('span',{},el('strong',{},names[0]+(names.length>1?' '+moreWords(names.length-1):'')),' · ',ruleSentence(r.rule)),
          String(r.why||''),copyNumbers(r.rule,String(r.severity)),r.applies?'':badge('Replaced','neutral'))))]:[empty('No rules apply','No active department rule matches this person on this day.',null,'rules')]),
        // The session decides the rest (its clinic, duty type, hours): the Schedule shows that day's sessions, and each one's Why.
        el('p',{},button('See on the Schedule',()=>{const id=who.input.value,on=day.input.value;(out.closest('dialog') as HTMLDialogElement|null)?.close();
          navigate(`schedule/week/${on}?person=${encodeURIComponent(id)}`);},'',{class:'link-button'})));
    }catch(error){if(current===request&&out.isConnected&&!sessionChanged(error)){out.replaceChildren(readFailed('the rules',error,run));}}
  };
  who.input.addEventListener('change',()=>void run());day.input.addEventListener('change',()=>void run());
  // A lookup: choosing a person or a day is not an unsaved change (the dialog closes without asking).
  who.wrap.dataset.lookup='';day.wrap.dataset.lookup='';
  void run();
  return el('div',{class:'which-rule'},el('div',{class:'form-grid'},pickPerson?who.wrap:null,day.wrap),out);
}
/** A person's side panel (People row, a person link anywhere): one hub with the tabs this role may read — Overview
 *  (contact, rank, clinics, skills, programmes; admins also roles), Schedule, Workload & fairness, Leave & requests, Rules,
 *  History and Access (person_profile.ts decides which). Forms
 *  opened from it stack on top; their save redraws the panel in place (same tab). Escape closes it and focus returns. */
/** New person, then their panel opens on them: rank, clinics and skills are added there with their own dates and
 *  approvals (nothing is granted here, and a rank is needed only where a duty or rule asks for one). */
function newPerson(){
  const before=new Set(rows('people').map(p=>p.id));
  catalogueForm(ctx(),'people',undefined,undefined,{note:'',run:()=>{const made=rows('people').find(p=>!before.has(p.id));if(made)personDetails(made);}});
}
function personDetails(person:Row,position?:PanelPosition,tab='overview'){
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10);
  const stateOf=(r:Row)=>r.revoked?'Revoked':r.ends_on&&r.ends_on<=today?'Ended':r.starts_on>today?'Upcoming':'Current';
  const tone:Record<string,Tone|''>={Upcoming:'info',Ended:'',Revoked:'neutral'};
  const order=['Current','Upcoming','Ended','Revoked'];
  const span=(r:Row)=>dateUnits([formatDate(r.starts_on),'– '+(r.ends_on?formatDate(lastIncludedDate(r.ends_on)):'no end')]);
  /** One kind of dated record (rank, clinic, skill, programme): current first, then upcoming, then the history. */
  const records=(p:Row,source:Catalogues,title:string,kind:string,key:string,target:string,can:boolean,add:HTMLElement|null,optional=false)=>{
    const named=(id:string)=>labelOf(source[target]?.find(r=>r.id===id));
    const entries=(source[kind]||[]).filter(r=>r.person_id===p.id).sort((a,b)=>order.indexOf(stateOf(a))-order.indexOf(stateOf(b))||String(b.starts_on).localeCompare(String(a.starts_on)));
    if(!entries.length&&optional&&!add)return null;
    // F14 (maintenance): which published duties a shorter clinic eligibility leaves without cover (read only, nothing saved);
    // Shared by End and Change dates. Rank end and withdrawal use the catalogue evaluator.
    // The server lists every duty left uncovered after the new dates, also ones this record never covered (after its old
    // end): only duties overlapping the days removed from THIS record count. null: unknown (no preview, or a cut list).
    const coverPreview=async(r:Row,dates:{starts_on?:string;ends_on?:string|null},why:string):Promise<Impacts|null>=>{
      const from=dates.starts_on??String(r.starts_on),until=dates.ends_on===undefined?r.ends_on??null:dates.ends_on,removed:[string,string][]=[];
      if(from>String(r.starts_on))removed.push([String(r.starts_on),from]);
      if(until&&(!r.ends_on||until<String(r.ends_on)))removed.push([until,String(r.ends_on||'9999-12-31')]);
      // Nothing removed (lengthening or no change): no duty can lose cover, so no request and never "may change".
      if(!removed.length)return {published_impact:{count:0,sessions:[]},draft_impact:{count:0,sessions:[]}};
      const res=await request<Impacts>(`/api/eligibilities/${encodeURIComponent(r.id)}/published-impact`,
        {method:'POST',body:JSON.stringify({...dates,reason:why.trim()||'Preview'})}).catch(()=>null);
      if(!res)return null;
      const z=zone(),hit=(x:Row)=>removed.some(([a,b])=>toLocal(String(x.start),z)<b+'T00:00'&&toLocal(String(x.end),z)>a+'T00:00');
      // An open-ended record losing everything after its new last day: the server's count IS the removed window (a cut list
      // keeps its count). A record that had an end may be counted past it, so a cut list there is unknown.
      const whole=!r.ends_on&&removed.length===1&&removed[0][1]==='9999-12-31';
      const only=(g?:Impact)=>{const list=g?.sessions||[];if(whole)return {count:Number(g?.count||0),sessions:list};
        if(Number(g?.count||0)>list.length)return undefined;const kept=list.filter(hit);return {count:kept.length,sessions:kept};};
      const published=only(res.published_impact),draft=only(res.draft_impact);
      return published&&draft?{published_impact:published,draft_impact:draft}:null;};
    const lostCover=(hit:number)=>hit?` ${plural(hit,'published duty','published duties')} now need${hit===1?'s':''} cover; the coordinators are told.`:'';
    const verb=kind==='grants'?'Revoke':'End',live=(r:Row)=>['Current','Upcoming'].includes(stateOf(r));
    const end=(r:Row)=>can&&kind!=='programme_memberships'&&live(r)?button(verb,()=>formDialog(`${verb} ${named(r[key])}`,'The earlier record stays in the history.',form=>{
      // SW3-D-06: a record that has not started ends on its first day at the earliest (today was refused: "End must follow start").
      const day=field('Last day','date',r.starts_on>today?String(r.starts_on):today,{required:true,min:r.starts_on}),reason=area('Reason','');reason.input.required=true;form.append(day.wrap,reason.wrap);
      // F14 (maintenance): before ending a clinic eligibility, which published duties it would leave (read only, nothing saved).
      if(kind==='eligibilities'){const impact=el('div',{class:'span-2','aria-live':'polite'});form.append(impact);let asked='';
        const preview=async()=>{const on=day.input.value;if(!on||on===asked)return;asked=on;
          const r=await coverPreview(r0,{ends_on:addDays(on,1)},reason.input.value);
          if(asked!==on||!r)return;
          impact.replaceChildren(...coverLines(r,zone()));};
        const r0=r;day.input.addEventListener('change',()=>void preview());queueMicrotask(()=>void preview());}
      const payload=()=>({ends_on:day.input.value?addDays(day.input.value,1):null,reason:reason.input.value});
      const saveRank=kind==='memberships'?configurationImpact(form,()=>({kind:'membership.end',payload:payload(),target_id:r.id,expected_revision:r.revision})):null;
      return async()=>{let hit=0;
        if(saveRank)await saveRank();
        else{const done=await command<{published_impact?:{count?:number}}>(kind==='eligibilities'?'eligibility.end':'grant.revoke',payload(),r.id,r.revision);
          hit=Number(done?.published_impact?.count||0);}
        await refresh();
        // Said, with Undo (P-10): the server's own reverse, through the one Undo registry (api.ts). F14: the published
        // duties it leaves without cover are said too (the coordinators are told separately).
        notify(`${named(r[key])} ${kind==='grants'?'revoked':'ends'} ${formatDate(day.input.value)}.${lostCover(hit)}`,hit?'warning':'success');};},verb),
      'ghost small',{'aria-label':`${verb} ${named(r[key])}`}):'';
    // DL-071: Extend is folded into Change dates (a later or empty last day), one path for a clinic's dates.
    // SW3-D-06: a rank, clinic or programme that has not started yet (added by mistake) is withdrawn, keeping its history (maintenance).
    const withdrawn:Record<string,string>={memberships:'membership.withdraw',eligibilities:'eligibility.withdraw',programme_memberships:'programme_membership.withdraw'};
    const withdrawable=(r:Row)=>can&&kind in withdrawn&&stateOf(r)==='Upcoming'&&(kind!=='programme_memberships'||programmeMembershipWritable(r));
    // A required reason and the armed press, then Undo (the server's restore of the same record), as Archive does.
    const withdraw=(r:Row)=>{
      const title=`Withdraw ${named(r[key])}?`,done=async()=>{await refresh();notify(`${named(r[key])} withdrawn.`,'success');};
      if(kind==='memberships'){
        formDialog(title,'The rank will no longer apply. Published duties stay in place.',form=>{
          const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;form.append(reason.wrap);
          const save=configurationImpact(form,()=>({kind:'membership.withdraw',payload:{reason:reason.input.value},target_id:r.id,expected_revision:r.revision}));
          return async()=>{await save();await done();};},'Withdraw');
        return;}
      confirmAction(title,"It won't apply. Published rotas keep it.",async reason=>{
        await command(withdrawn[kind],{reason},r.id,r.revision);await done();},'Withdraw',undefined,false,false);};
    // A skill can be limited to one clinic; the clinic is part of what it says.
    const scope=(r:Row)=>kind==='grants'?(r.service_id?labelOf(source.services?.find(s=>s.id===r.service_id)):'All clinics'):'';
    // Change dates (M1): the start and last day, by the same command and roles the Bulk editor uses (its update_dates).
    const datesCommand=['memberships','eligibilities','grants'].includes(kind)?`${kind==='memberships'?'membership':kind==='eligibilities'?'eligibility':'grant'}.update_dates`:'';
    // A range that removes days is never saved silently: Save is armed and names the loss (the server's preview count for
    // a clinic; memberships and grants have no preview yet, so it says what to check). Lengthening saves at once.
    const changeable=(r:Row)=>!!datesCommand&&permits(...(editRoles(kind)||[]))&&!r.revoked;
    const dates=(r:Row)=>formDialog(`Change dates · ${named(r[key])}`,'',form=>{
      const first=field('First day','date',String(r.starts_on),{required:true}),last=field('Last day (included, optional)','date',String(r.ends_on||''));lastDayIncluded(last.input);  // .value stays the stored end
      const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;reason.wrap.classList.add('span-2');
      const impact=el('div',{class:'span-2','aria-live':'polite'});
      form.append(dateRange('Dates',first,last),reason.wrap,impact);
      const ends=()=>last.input.value||null;
      const removesEnd=()=>{const e=ends();return !!e&&(!r.ends_on||e<String(r.ends_on));},removesStart=()=>first.input.value>String(r.starts_on);
      // null: not known (no server preview, or the preview is still being read).
      let lost:number|null=null,asked='';
      const submit=()=>form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]')||null;
      const sync=()=>{const s=submit();if(!s)return;const shorter=removesEnd()||removesStart();
        relabel(s,!shorter||lost===0?'Save':lost?`Save · ${plural(lost,'published duty','published duties')} ${agree(lost,'loses','lose')} cover`:'Save · Published cover may change');
        if(shorter&&lost===null)impact.replaceChildren(note('Check the published schedule for this person before confirming.','warning'));};
      const preview=async()=>{const e=ends(),key=`${first.input.value}|${e}`;if(key===asked)return;asked=key;lost=null;impact.replaceChildren();sync();
        if(kind!=='eligibilities'||!(removesStart()||removesEnd()))return;
        const res=await coverPreview(r,{...(first.input.value!==r.starts_on?{starts_on:first.input.value}:{}),...(e!==(r.ends_on||null)?{ends_on:e}:{})},reason.input.value);
        if(asked!==key||!res)return;
        lost=Number(res.published_impact?.count||0);impact.replaceChildren(...coverLines(res,zone()));sync();};
      for(const f of [first,last])f.input.addEventListener('change',()=>void preview());
      queueMicrotask(()=>{const s=submit();if(s)armed(s,'Confirm?',()=>(removesEnd()||removesStart())&&lost!==0);void preview();});
      return async()=>{const e=ends();
        const patch={...(first.input.value!==r.starts_on?{starts_on:first.input.value}:{}),...(e!==(r.ends_on||null)?{ends_on:e}:{})};
        if(!Object.keys(patch).length)throw new Error('Nothing changed.');
        const done=await command<{published_impact?:{count?:number}}>(datesCommand,{...patch,reason:reason.input.value},r.id,r.revision);await refresh();
        const hit=Number(done?.published_impact?.count||0);notify(`${named(r[key])} dates changed.${lostCover(hit)}`,hit?'warning':'success');};},'Save');
    // DL-071: one visible action per row (End / Revoke); Change dates and Withdraw wait in the row's ⋯ menu.
    const more=(r:Row)=>{const what=named(r[key]),items=[...(changeable(r)?[{label:'Change dates',icon:'edit' as const,run:()=>dates(r)}]:[]),
      ...(withdrawable(r)?[{label:'Withdraw',icon:'close' as const,run:()=>withdraw(r)}]:[])];
      if(!items.length)return '';
      const b:HTMLButtonElement=opens(iconButton('More for '+what,'dots',()=>menu(b,'More for '+what,items),{class:'bare','data-tip':'More'}),'menu');return b;};
    const row=(r:Row)=>profileRow(named(r[key]),scope(r),span(r),stateOf(r)==='Current'?'':badge(stateOf(r),tone[stateOf(r)]),end(r),more(r));
    // SW3-D-14: ended and revoked records wait behind "Show ended (N)": what holds now comes first, never under 20 ended.
    const now=entries.filter(live),past=entries.filter(r=>!live(r));
    const pastList=el('ul',{class:'ph-rows ph-past'},...past.map(row));pastList.hidden=true;
    const toggle:HTMLButtonElement|null=past.length?button(`Show ended (${past.length})`,()=>{pastList.hidden=!pastList.hidden;
      toggle!.textContent=pastList.hidden?`Show ended (${past.length})`:'Hide ended';toggle!.setAttribute('aria-expanded',String(!pastList.hidden));},'link-button',{'aria-expanded':'false'}):null;
    return profileSection(title,add,...(now.length?[el('ul',{class:'ph-rows'},...now.map(row))]:[emptyLine(past.length?'None now':'None')]),...(toggle?[toggle,pastList]:[]));
  };
  const parts=(p:Row):ProfileParts=>({
    contact:()=>{
      // The card (person_card.ts): today, Call / Message / Copy and More, as the server permits this reader.
      const card=personCard(ctx(),p,{sub:text=>panelHead?.(fullNameOf(p),text),tab:k=>panelTab?.(k),
        leave:may('availability.record')?()=>availabilityForm(ctx(),false,{person_id:p.id}):undefined});
      if(peopleDirectory())return card;
      const facts:[string,Child][]=[];
      for(const [label,value,shown] of [['Employee number',p.staff_number,p.staff_number],['Short code',true,shortCode(p)],['Tags',(p.tags||[]).length,(p.tags||[]).join(', ')]] as [string,unknown,Child][])
        if(value)facts.push([label,shown]);
      // Details (P-01): the person's one Edit sits with what it edits (code, tags, directory).
      return el('div',{class:'ph'},card,profileSection('Details',permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR)?button('Edit',()=>catalogueForm(ctx(),'people',p),'ghost small',{'aria-label':'Edit '+fullNameOf(p)}):null,ruleFacts(facts)));
    },
    records:(source,readOnly)=>{
      const can=(...roles:string[])=>!readOnly&&permits(...roles),add=(label:string,run:()=>void,ok:boolean,name=label)=>ok?button(label,run,'ghost small',{'aria-label':name}):null;
      const clinical=[records(p,source,'Clinics','eligibilities','service_id','services',can(CAPABILITY.CREDENTIALER),add('Authorise',()=>relationshipForm(ctx(),'eligibility',p),can(CAPABILITY.CREDENTIALER),'Authorise eligibility')),
        records(p,source,'Skills','grants','capability_id','capabilities',can(CAPABILITY.CREDENTIALER),add('Grant',()=>relationshipForm(ctx(),'grant',p),can(CAPABILITY.CREDENTIALER),'Grant skill'))];
      const rank=[records(p,source,'Rank','memberships','category_id','categories',can(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR),add('Add rank',()=>relationshipForm(ctx(),'membership',p),can(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR))),
        records(p,source,'Programmes','programme_memberships','programme_id','programmes',!readOnly&&catalogueEditable('programme_memberships'),null,true)];
      // A credentialer's own work comes first for them.
      // Sign-ins and roles are on the Access tab.
      return (permits(CAPABILITY.CREDENTIALER)&&!permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR)?[...clinical,...rank]:[...rank,...clinical]).filter(Boolean) as Node[];
    },
    rules:!peopleDirectory()&&canLookupRules()?()=>{
      const own=rows('exceptions').some(r=>r.person_id===p.id);
      return el('div',{class:'ph'},whichRuleLookup(p.id,false),own?profileSection('Exceptions','',exceptionsTable(refresh,p.id)):'');
    }:undefined,
    feed:events=>auditFeed(events),
  });
  let shown=tab,panelHead:((title:string,sub:string)=>void)|null=null,panelTab:((key:string)=>void)|null=null;
  const body=(p:Row)=>{const list=personProfilePanels(ctx(),p,parts(p));if(!list.some(x=>x[0]===shown))shown=list[0][0];
    // Only Overview (a colleague's contact details): no tab strip.
    return list.length>1?personTabs('About '+fullNameOf(p),list,shown,k=>{shown=k;
      // A link may open a tab (?tab=facts&fact=…); choosing another tab drops that link from the address.
      if(view==='people'&&routeQuery.has('tab')&&routeQuery.get('tab')!==k){routeQuery.delete('tab');routeQuery.delete('fact');window.history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;}}):list[0][2]();};
  const sub=(p:Row)=>[p.title,sectionLabel(p)].filter(Boolean).join(' · ');  // the recorded title, not the rank again (P-02)
  dockPanel({key:'person',className:'person-panel',title:fullNameOf(person),sub:sub(person),position,mark:rowMark(person.id),width:640,
    onClose:leaving=>{if(personPanel?.id===person.id)personPanel=null;if(!leaving&&view==='people'&&routeQuery.get('open')===person.id){routeQuery.delete('open');routeQuery.delete('tab');routeQuery.delete('fact');window.history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;}},
    build:({panel:aside,body:root,close,setHead})=>{
      // DL-027: a card opened over the list is its own history entry, so Back closes it (and stays on People); moving
      // to another person (↑ ↓) or arriving by its address replaces it.
      if(view==='people'){const had=routeQuery.get('open');routeQuery.set('open',person.id);
        window.history[had?'replaceState':'pushState'](null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;}
      panelHead=setHead;
      // Another tab, chosen from inside a tab (the card's Rules): the tab strip's own button, so it behaves as a click.
      panelTab=key=>root.querySelector<HTMLElement>(`[role=tab][id$="-${key}-tab"]`)?.click();
      root.append(body(person));
      personPanel={id:person.id,redraw:()=>{
        // Opened from a page without the people list (Home, the Schedule): the person it opened on stays.
        const list=peopleDirectory()?directoryRows:rows('people'),p=list?list.find(x=>x.id===person.id):person;if(!p||!aside.isConnected){close();return;}
        const inside=root.contains(document.activeElement);root.replaceChildren(body(p));setHead(fullNameOf(p),sub(p));
        // The control that opened a form may be gone: focus lost with it goes to the open tab rather than the page.
        requestAnimationFrame(()=>{const lost=!document.activeElement||document.activeElement===document.body;if(aside.isConnected&&(inside||lost)&&!root.contains(document.activeElement))(root.querySelector<HTMLElement>('[role=tab][aria-selected=true]')||root.querySelector<HTMLElement>('a,button'))?.focus();});
      }};
    }});
}
/** Programmes: the rotation board first (trainees on a timeline), then the training records it is built from. A trainee
 *  without a planning role sees only their own board (My training). What blocks duties is in the page's help (?). */
const PROGRAMME_KINDS=['board','programmes','programme_memberships','cohorts','rotation_types','rotation_templates','rotations','mentorships','programme_calendar_entries'];
async function programmesPage(target:HTMLElement){
  if(!permits(...paths.programmes.roles)){await rotationBoard(target,{ctx:ctx(),zone:zone(),mine:true});return;}
  await renderCatalogue(target,PROGRAMME_KINDS);}

/** A chip for a list: the first two names and a count, with the full list on hover. */
/** Planner shortcuts: each opens the one canonical form (session, duty type, place, clinic, person, leave, rule, exception), prefilled from what was clicked. */
function plannerShortcuts(data:WorkspaceData,t:MenuTarget,draft:boolean):MenuEntry[]{
  const c=ctx(),s=t.shift,person=t.person_id?rows('people').find(p=>p.id===t.person_id):undefined,out:MenuEntry[]=[];
  // First: the rules that apply to what was pointed at (pins the rules panel).
  const rt:RuleTarget|null=person&&s?{kind:'assignment',person:person.id,shift:s.id,day:t.day,label:fullNameOf(person),sub:s.type_snapshot.name}:person?{kind:'person',person:person.id,label:fullNameOf(person),shifts:data.shifts.filter(x=>x.participants.some(p=>p.person_id===person.id)).map(x=>x.id)}
    :s&&t.shifts&&t.shifts.length>1?{kind:'row',type:s.assignment_type_id,location:s.location_id,label:s.type_snapshot.name,shifts:t.shifts.map(x=>x.id)}:s?{kind:'session',shift:s.id,label:s.type_snapshot.name}
    :t.day?{kind:'day',day:t.day,days:[t.day],label:dayTitle(t.day),shifts:(t.shifts||[]).map(x=>x.id)}:null;
  if(rt)out.push({label:'Rules that apply here',icon:'rules',hint:'Which rule wins, and change it',run:()=>pinRules(rt,'rules')});
  // Bulk: pick every session in this row or day, then act on them from the selection bar.
  const pick=t.shifts||[];
  const selectAll:MenuEntry[]=!person&&draft&&permits(CAPABILITY.COORDINATOR)&&pick.length?[{label:`Select ${pick.length===1?'this session':'these '+pick.length+' sessions'}`,icon:'check',hint:'Then close, lock, remove or copy together',run:()=>{pick.forEach(x=>selected.add(x.id));
    // In place (SI-14): the boxes tick and the bar appears; the page is not drawn again.
    document.querySelectorAll<HTMLInputElement>('input.wk-check').forEach(b=>{if(selected.has(b.dataset.shift||''))b.checked=true;});updateSelectionBar();}}]:[];
  const find=(kind:string,id:string)=>rows(kind).find(r=>r.id===id);
  const weekday=(day:string)=>String((new Date(day+'T12:00:00Z').getUTCDay()+6)%7);
  const dayName=(day:string)=>new Intl.DateTimeFormat('en-GB',{weekday:'long',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));
  if(person){
    out.push({label:'View person',icon:'people',hint:'Work, requests and related records',run:()=>personDetails(person)});
    if(permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR))out.push({label:'Edit '+person.display_name,icon:'people',hint:'Colour, code, tags',run:()=>catalogueForm(c,'people',person)});
    if(permits(CAPABILITY.COORDINATOR))out.push({label:'Record leave',icon:'leave',hint:t.day?formatDate(t.day):'',run:()=>availabilityForm(c,false,{person_id:person.id,day:t.day})});
    if(permits(CAPABILITY.COORDINATOR)&&rows('policies').some(r=>r.status==='active'&&allowsException(r)))out.push({label:'Request a rule exception',icon:'shield',hint:'Approved separately',run:()=>exceptionForm(c,{person_id:person.id,day:t.day})});
    if(may('rule.propose'))out.push({label:'Add a rule for '+person.display_name,icon:'rules',hint:'Needs approval',run:()=>policyForm(c,'',{person_id:person.id,title:person.display_name+' · '})});
    return out;
  }
  if(s){
    if(draft&&permits(CAPABILITY.COORDINATOR))out.push({label:'Add this session on another day',icon:'plus',hint:'Same duty, place and times',run:()=>shiftForm(c,data,undefined,{shift:s,day:t.day})});
    const type=find('assignment_types',s.assignment_type_id),place=find('locations',s.location_id);
    if(permits(CAPABILITY.ADMIN)){
      if(type)out.push({label:'Edit duty type',icon:'settings',hint:'Staffing, timing, colour',run:()=>catalogueForm(c,'assignment_types',type)});
      if(place)out.push({label:'Edit location',icon:'settings',hint:labelOf(place),run:()=>catalogueForm(c,'locations',place)});
      for(const id of s.service_ids as string[]){const svc=find('services',id);if(svc)out.push({label:'Edit service',icon:'settings',hint:labelOf(svc),run:()=>catalogueForm(c,'services',svc)});}
    }
    if(may('rule.propose'))out.push({label:'Add a rule for this duty',icon:'rules',hint:'Needs approval',run:()=>policyForm(c,'',{assignment_type_id:s.assignment_type_id,title:s.type_snapshot.name+' · '})});
    return [...selectAll,...out];
  }
  if(t.day){
    if(draft&&permits(CAPABILITY.COORDINATOR))out.push({label:'Add session on this day',icon:'plus',run:()=>shiftForm(c,data,undefined,{day:t.day})});
    if(permits(CAPABILITY.COORDINATOR))out.push({label:'Record leave on this day',icon:'leave',run:()=>availabilityForm(c,false,{day:t.day})});
    if(may('rule.propose'))out.push({label:`Add a rule for ${dayName(t.day)}s`,icon:'rules',hint:'Needs approval',run:()=>policyForm(c,'',{weekdays:[weekday(t.day!)],title:dayName(t.day!)+' · '})});
  }
  return [...selectAll,...out];
}
/** Colour a scope chip with the colour of the record it names (duty types keep their own colour). */
const tinted=(b:HTMLElement,kind:string,id:string)=>{if(!['categories','services','assignment_types','people'].includes(kind))return b;const row=rows(kind).find(r=>r.id===id);
  b.classList.add('duty-'+(kind==='assignment_types'?typeColour(id):kind==='categories'?rankColour(id):colourOf(row,id)));b.setAttribute('data-lg-self','');return b;};  // the chip's words name it
// The chip's name lists everyone, so its "+N" only shows.
const listChip=(label:string,names:string[])=>{const b=badge(`${label}: ${names.slice(0,2).join(', ')}`);if(names.length>2)b.append(' ',overflowMore(names.length-2,label,{as:'hidden',cls:''}));b.setAttribute('data-tip',names.join(', '));b.setAttribute('aria-label',`${label}: ${names.join(', ')}`);return b;};
/** The number each rule kind is about (what a line of the rule changes): one list in forms.ts (RULE_NUMBER_FIELD). */
const VALUE_KEY:Record<string,string>=Object.fromEntries(Object.entries(RULE_NUMBER_FIELD).map(([k,[f]])=>[k,f]));
const VALUE_LABEL:Record<string,string>=Object.fromEntries(Object.entries(RULE_NUMBER_FIELD).map(([k,[,l]])=>[k,l]));
let retiredRules:Row[]|null=null;
let effectiveSources:Record<string,Row>={};
let retiredVisit=0;
const referencePolicy=(id:string)=>effectiveSources[id]||find('policies',id)||(retiredRules||[]).find(p=>p.id===id);
const referenceContext=():Context=>({...ctx(),catalogues:{...catalogues,policies:rows('policies').map(p=>referencePolicy(p.id)||p)}});
/** Retired and declined rules, read once per visit to the Rules page (History, and where a rule came from). */
const loadRetired=async():Promise<Row[]>=>{
  if(retiredRules)return retiredRules;
  const visit=retiredVisit;
  // The shared API reader already deduplicates in-flight requests. Keep only this visit's resolved data.
  // Retained history is paged (stable id order): read every page, merging each page's effective sources.
  const policies:Row[]=[],effective:Record<string,Row>={};
  for(let more=true;more;){const page=await get<{policies:Row[];effective?:Record<string,Row>;has_more?:boolean}>('/api/policies?status=retired&effective=true'+(policies.length?`&offset=${policies.length}`:''));
    policies.push(...page.policies);Object.assign(effective,page.effective);more=!!page.has_more&&page.policies.length>0;}
  if(visit===retiredVisit){retiredRules=policies;effectiveSources=effective;}return policies;
};
/** Findings keep their approved source identity; its reviewed destination is navigation metadata only. */
async function openRuleSource(id:string,personId?:string){
  let policy=find('policies',id);if(!policy?.definition)policy=(await loadRetired()).find(p=>p.id===id);
  if(!policy&&personId){const facts=await get<{active:Row[];proposed:Row[];history:Row[]}>(`/api/people/${personId}/scheduling-facts`);
    if([...facts.active,...facts.proposed,...facts.history].some(f=>f.id===id)){navigate(routeHash('people',[],{open:personId,tab:'facts',fact:id}).slice(1));return;}}
  if(!policy?.definition){notify('This rule is no longer available. Refresh to see its source.','warning');return;}
  // A catalogue rule in force opens as its catalogue rule page (rules/<entry>), where its lines, checks and history are;
  // the finding, fix panel and Resolve issues name it by policy id, which is not a catalogue address.
  const entry=policy.status!=='retired'&&ruleCatalogueOn()?catalogueEntryOf(policy.definition.rule):undefined;
  if(entry){navigate(`rules/${encodeURIComponent(entry.id)}`);return;}
  if(policy.status==='retired'&&permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD)){
    const {records}=await conversions();
    const active=await Promise.all(records.filter(r=>r.status==='active').map(r=>conversion(r.id)));
    for(const {record} of active){const binding=(record.plan.bindings as Row[]).find(b=>b.source_policy_id===id);if(binding){
      const targets=new Map((record.plan.targets as Row[]).map(t=>[t.row.id,t.catalogue_id])),facts=new Map((record.plan.scheduling_facts as Row[]).map(f=>[f.id,f]));
      policy={...policy,_conversion_id:record.id,_destinations:binding.destinations.map((d:Row)=>({...d,catalogue_id:targets.get(d.id),fact:facts.get(d.id)}))};break;}}
  }
  // A proposal opened from its source is reviewed there too (its proposer only under a self-approval permission).
  const waiting=policy;
  const review=waiting.status==='proposed'&&may('policy.activate')?()=>ruleReview(waiting,permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),mayDecide(me!,'rule.activate',waiting.created_by===me!.identity_id),
    refresh,name,zone(),false,[],labelOf(waiting),'',ctx()):undefined;
  ruleSheet(policy,labelOf(policy),undefined,review);
}
const ruleDetailView=new Map<string,string>();
// shortcut: unused since the rule tabs moved to tabs() (S8); delete with its clear() once wave-1 names-c19 has merged.
const rulePanelIds=new Map<string,number>();
/** Short states are badges; dates, scope and ownership keep visible labels. */
/** Visible tabs retain each rule's selected panel; expensive history is loaded only when requested. */
function rulePanels(key:string,details:HTMLElement,history:()=>HTMLElement,source:()=>HTMLElement):HTMLElement{
  // The shared tabs, in the rule's own frame (.rule-detail > tabs + panels: the docked rule sheet lays these out).
  const set=sharedTabs('Rule information',[['details','Details',()=>details],['history','History',history],['source','Source',source]],
    ruleDetailView.get(key)||'details',v=>ruleDetailView.set(key,v),{activation:'auto',cls:'rule-tabs rule-detail-tabs',panelsCls:'rule-detail-panels'});
  return el('div',{class:'rule-detail'},...set.children);
}
const personIds=(scope:Row)=>scope?.person_ids?.length?scope.person_ids as string[]:scope?.person_id?[scope.person_id as string]:[];
/** Limits overtime can depend on (mandatory maximum quotas, next-day combinations, weekends in a row). */
const isCap=(p:Row)=>p.status==='active'&&p.active!==false&&p.definition?.severity==='mandatory'&&(p.definition.rule.kind==='block_quota'&&['maximum','range'].includes(p.definition.rule.comparison)||['forbidden_transition','max_consecutive_weekends'].includes(p.definition.rule.kind));
/** The limits in force now: active ones, and the rules an active one was grouped from (the same test auto-schedule uses). */
function limitsInForce(){const caps=rows('policies').filter(isCap);return new Set([...caps.map(p=>p.id),...caps.flatMap(p=>p.definition.merged_ids||[])]);}
/** What is wrong with an active rule that the page can tell by itself. */
function problemsOf(p:Row,inForce:Set<string>):string[]{
  const r=p.definition?.rule;
  return p.status==='active'&&r?.kind==='allocation_overtime'&&(r.required_policy_ids||[]).some((id:string)=>!inForce.has(id))?['Overtime switched off: the limits it needs are no longer in force.']:[];
}
/** The values a rule names for a field (its conditions' “is any of” lines, its older flat fields and its lines). */
function mentions(p:Row,field:string):string[]{
  const d=p.definition,out:string[]=[];
  const walk=(t:any)=>{for(const c of t?.conditions||[]){if('conditions' in c)walk(c);else if(!negativeOp(c.op)&&c.field===field)out.push(...literalValues(c));}};
  walk(scopeAsConditions(d.scope||{}));
  for(const l of d.per_person||[]){
    if(field==='person_ids'&&l.person_id)out.push(l.person_id);if(field==='programme_ids'&&l.programme_id)out.push(l.programme_id);
    if(field==='service_ids')out.push(...(l.service_ids||[]));if(field==='assignment_type_ids')out.push(...(l.assignment_type_ids||[]));}
  if(field==='person_ids')out.push(...(d.rule.person_ids||[]));
  return out;
}
/** A “Who” filter (the SSOT condition tree) against what a rule names: “Person is any of Kopofiku” keeps rules naming Kopofiku. */
function namesMatch(p:Row,tree:any):boolean{
  return !tree?.conditions?.length||treeMatches(tree,c=>lineMatches(c,mentions(p,c.field)));
}
/** The numbers a rule uses: its own (unless every line has one) and each line's. */
function numbersOf(p:Row,key:string){
  const d=p.definition,lines:Row[]=d.per_person||[];
  return [...(!d.listed_only||lines.some(l=>l.amount==null)?[Number(d.rule[key])]:[]),...lines.filter(l=>l.amount!=null).map(l=>Number(l.amount))].filter(v=>!Number.isNaN(v));
}
/** A rule's conditions in plain words ("Rank is any of R1, R2 · except Consultants"); '' when it has none. */
// SW3-D-04: values read through the one reference resolver (namer), so a tag reads as its #name, never its id.
const conditionWords=(scope:Record<string,any>)=>conditionsText(scope?.conditions,namer(ctx()).label);
/** Who a run schedules in one short line ("Everyone", "9 ranks · 80 people"), the full scope in its tip and the run's
 *  Review reviewed. */
const scopeLine=(scope:Record<string,any>|null|undefined)=>{const full=conditionWords(scope||{});
  return el('span',{class:'run-scope',...(full?{'data-tip':full}:{})},conditionsSummary(scope?.conditions,namer(ctx()).label)||'Everyone');};
/** Who and when a line applies to, ignoring its number and dates: lines with the same key replace each other. */
const targetKey=(scope:Row)=>JSON.stringify([personIds(scope).slice().sort(),[...(scope.category_ids||[]),...(scope.category_id?[scope.category_id]:[])].sort(),
  [...(scope.tags||[])].map((t:string)=>t.toLowerCase()).sort(),[...(scope.capability_ids||[])].sort(),scope.programme_id||'',scope.rotation_type_id||'',scope.assignment_type_id||'',
  [...(scope.service_ids||[]),...(scope.service_id?[scope.service_id]:[])].sort(),scope.location_id||'',[...(scope.weekdays||[0,1,2,3,4,5,6])].sort(),scope.start_time||'00:00',scope.end_time||'24:00',scope.conditions||null]);
const daysOf=(scope:Row)=>{const d:number[]=scope?.weekdays||[0,1,2,3,4,5,6];return d.length===7?'Every day':weekdayNames(d,true);};
/** Chips for who and where a rule (or one line of it) applies. */
function scopeChips(scope:Row):HTMLElement[]{
  const chips=[['person_id','people','Person'],['category_id','categories','Rank'],['programme_id','programmes','Programme'],['rotation_type_id','rotation_types','Rotation type'],
    ['assignment_type_id','assignment_types','Duty type'],['service_id','services','Service'],['location_id','locations','Location']]
    .filter(([key])=>scope[key]).map(([key,kind,label])=>tinted(badge(label+': '+name(kind,scope[key])),kind,scope[key]));
  if(scope.person_ids?.length)chips.push(tinted(listChip(scope.person_ids.length===1?'Person':`${scope.person_ids.length} people`,scope.person_ids.map((id:string)=>name('people',id))),'people',scope.person_ids[0]));
  if(scope.tags?.length)chips.push(listChip(scope.tags.length===1?'Tag':'Tags',scope.tags));
  if(scope.capability_ids?.length)chips.push(listChip('Skills',scope.capability_ids.map((id:string)=>name('capabilities',id))));
  if(scope.category_ids?.length){const c=listChip('Ranks',scope.category_ids.map((id:string)=>name('categories',id)));chips.push(scope.category_ids.length===1?tinted(c,'categories',scope.category_ids[0]):c);}  // several ranks: no one rank's tint
  if(scope.service_ids?.length)chips.push(tinted(listChip('Clinics',scope.service_ids.map((id:string)=>name('services',id))),'services',scope.service_ids[0]));
  if(conditionWords(scope))chips.push(badge(conditionWords(scope)));
  if((scope.weekdays||[0,1,2,3,4,5,6]).length<7)chips.push(badge(daysOf(scope)));
  if(scope.start_time&&scope.start_time!=='00:00'||scope.end_time&&scope.end_time!=='24:00')chips.push(badge((scope.start_time||'00:00')+'–'+(scope.end_time||'24:00')));
  if(!chips.length)chips.push(badge('Everyone · all duties'));
  return chips;
}
/** One plain sentence for what a rule asks (rule_view.ts ruleSentence, with this page's names). */
const ruleSentence=(rule:Row,definition?:Row)=>sentenceOf(rule,definition,name);
/** A quota's facts as labelled values: its limits (minimum, maximum, target) in one row, what it counts and its period. */
function quotaFacts(rule:Row):[string,Child][]{
  const unit=String(quotaUnits[rule.metric]||rule.metric).toLowerCase(),t=rule.comparison==='target'?rule.amount:rule.target_amount;
  const limits=[['minimum','range'].includes(rule.comparison)?`Minimum ${rule.amount}`:'',rule.comparison==='range'?`Maximum ${rule.maximum_amount}`:rule.comparison==='maximum'?`Maximum ${rule.amount}`:'',
    t!=null?`Target ${t}${rule.target_strength?` (${rule.target_strength} strength)`:''}`:''].filter(Boolean).join(' · ');
  const per=quotaPeriod(rule);
  const types=(rule.assignment_type_ids||[]).map((id:string)=>name('assignment_types',id)).join(', ');
  return [['Limits',`${limits} ${unit}`],
    ['Counts',[types||'Every duty type',rule.overtime==='exclude'?'overtime not counted':rule.overtime==='only'?'overtime only':'overtime included'].join(' · ')],
    ['Period',`${formatDate(rule.starts_on)} – ${formatDate(lastIncludedDate(rule.ends_on))} · ${per}`]];
}
const approvalPending=(a:Row|undefined)=>pendingApproval(a,me?.identity_id);
const ruleState=(p:Row)=>p.status==='proposed'?statusLabel('proposed'):p.status==='active'?'Active':p.approved_by?'Retired':'Declined';
const ruleDateParts=(p:Row)=>[formatDate(p.starts_on),p.ends_on?formatDate(lastIncludedDate(p.ends_on)):'no end'];
const ruleDates=(p:Row)=>ruleDateParts(p).join(' – ');
const ruleDatesNode=(p:Row)=>dateUnits(ruleDateParts(p).map((part,i)=>(i?'– ':'')+part),'rule-valid');
/** A draft's rule map, once per Rules visit. */
let draftMaps=new Map<string,Promise<RuleMap>>();
const draftMap=(id:string)=>{let m=draftMaps.get(id);if(!m){m=get<RuleMap>(`/api/workspaces/${id}/rule-map`);draftMaps.set(id,m);}return m;};
/** The drafts the Rules page reports on (newest first) and the one shown: the first covering today, else the draft open
 *  in Schedule, else the most recent. Once per visit; a rule sheet may
 *  switch it for the rest of the visit. Null: no draft, or a role that does not read rotas. */
let draftChoice:Promise<{drafts:Row[];chosen:string}|null>|null=null;
const currentDrafts=()=>draftChoice||=(permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)?get<{workspaces:Row[]}>('/api/workspaces?limit=50&offset=0').then(r=>{
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10);
  const drafts=r.workspaces.filter(w=>w.status==='draft').sort((a,b)=>String(b.starts_on).localeCompare(String(a.starts_on)));
  if(!drafts.length)return null;
  // F-R9: the rotas being planned (not amendments of a published one) that have not ended, the nearest first; then the
  // draft open on the Schedule; then the others. An old amendment covering today says nothing about the lines planned.
  const planned=(w:Row)=>!w.base_publication_id&&today<w.ends_on,open=(w:Row)=>w.id===workspaceId;
  const order=[...drafts.filter(planned).reverse(),...drafts.filter(w=>!planned(w)&&open(w)),...drafts.filter(w=>!planned(w)&&!open(w))];
  // Chosen by order alone; no draft's rule map is read to choose (a rule page reads none until asked).
  return {drafts,chosen:order[0].id};}).catch(()=>null):Promise.resolve(null));
/** People each rule reaches today (GET /api/policies/reach), kept between draws of the Rules page. */
let reachNow:Record<string,number>={};
/** A section of the rule sheet: its heading, an optional action, then its content. */
const sheetSection=(title:string,action:Child,...content:Child[])=>el('section',{class:'rl-sec','aria-label':title},el('div',{class:'rl-sec-head'},el('h3',{},title),action),...content);
/** One rule's sheet: Details (who and when, numbers, exceptions, how it fares in the current draft, facts), History
 *  and Source, and what can be done with it in the footer. */
/** "In <draft>": how a rule fares in a draft (nothing when there is none, or the rule is not checked there); with several
 *  drafts the section's head switches between them. One section for the rule sheet and the catalogue rule page. */
function draftHealth(policy:Row):HTMLElement{
  let health:HTMLElement=el('div',{class:'rl-health-slot',hidden:true});
  // A swap that removes the focused control (the pressed button) keeps the keyboard in place: on the new section's heading.
  const swap=(next:HTMLElement)=>{const had=health.contains(document.activeElement);health.replaceWith(next);health=next;
    const h=next.querySelector<HTMLElement>('h3');if(had&&h){h.tabIndex=-1;focusQuietly(h);}};
  const draftOf=(found:{drafts:Row[];chosen:string})=>found.drafts.find(w=>w.id===found.chosen)!;
  // On demand. The page shows which draft and one button; the draft's rule map and drift are read only when asked
  // (they cost ~30 s at department scale, and a rule page must not read a draft by itself).
  const offer=(found:{drafts:Row[];chosen:string})=>swap(sheetSection('In '+labelOf(draftOf(found)),
    button('Show effect',()=>inDraft(found),'secondary small',{'aria-label':`Show effect in ${labelOf(draftOf(found))}`})));
  const inDraft=(found:{drafts:Row[];chosen:string})=>{
    swap(sheetSection('In '+labelOf(draftOf(found)),'',skeleton('lines',`Loading ${labelOf(draftOf(found))} effect…`)));
    void draftMap(found.chosen).then(map=>{
    const entry=map.rules.find(r=>r.id===policy.id),draft=draftOf(found);
    // A failure is drawn in this section (never a page refresh: a lasting 500 would refresh for good).
    requestBaselineDrift(draft,()=>void refresh(),n=>{const actions=health.querySelector('.rl-sec-actions');
      actions?.querySelector('.baseline-drift')?.remove();actions?.prepend(n);});
    const drift=baselineDriftFor(draft),driftAction=drift?.changes.length?button(departmentChanges(drift.changes.length),
      ()=>void refreshDraftBaseline(draft,refresh).catch(reportError),'secondary small',{'aria-label':driftName(drift.changes.length)}):'';
    // Asked for: the loading section is always replaced by a result (never left loading), so only a closed page stops here.
    if(!health.isConnected)return;
    const pick=found.drafts.length>1?select('Draft',found.drafts.map(w=>({value:w.id,label:labelOf(w)})),found.chosen):null;
    pick?.wrap.classList.add('rl-draft-pick');pick?.input.setAttribute('data-lookup','');  // a view choice: not an unsaved change
    pick?.input.addEventListener('change',()=>{found.chosen=pick.input.value;inDraft(found);});
    const next=sheetSection('In '+labelOf(draft),el('div',{class:'rl-sec-actions'},driftAction,pick?.wrap||'',showOnPlanner([policy],true)||''),
      !map.shifts.length?el('p',{class:'hint'},'No duties yet.'):entry?healthStrip(entry,map.days,map.fresh,reachNow[policy.id]??null):el('p',{class:'hint'},'This rule is not checked in this draft.'));
    swap(next);}).catch(e=>{
      // A failure stays in this section with the shared failure note and its Retry (the failed read is not kept).
      draftMaps.delete(found.chosen);
      if(alive(health))swap(sheetSection('In '+labelOf(draftOf(found)),'',readFailed(`${labelOf(draftOf(found))} effect`,e,()=>inDraft(found))));});};
  // Offered once the slot is on the page: a page is built detached and shown after its skeleton (the catalogue rule page),
  // so the slot is awaited frame by frame. shortcut: gives up after ~10 s unattached (a page replaced before it showed).
  const attached=(found:{drafts:Row[];chosen:string},frames=600)=>requestAnimationFrame(()=>health.isConnected?offer(found):frames>0&&attached(found,frames-1));
  // Only an active rule is in a draft's map: a rule waiting for approval, retired or switched off keeps the hidden slot.
  if(policy.status==='active'&&policy.active!==false)void currentDrafts().then(found=>{if(found)attached(found);});
  return health;
}
function ruleSheet(policy:Row,title:string,inForce:Set<string>=limitsInForce(),review?:()=>unknown,position?:PanelPosition){
  const definition=policy.definition,rule=definition.rule,scope=definition.scope||{};
  const canEdit=policy.status!=='retired'&&may('rule.propose');
  rulePanel([policy],title,labelOf(policy),position,({body,close})=>{
    const change=()=>ruleChangeForm(ctx(),policy);
    const edit=()=>canEdit?compact(button('Edit',change,'ghost small'),'edit'):'';
    const panel=el('div',{class:'rule-detail-content rl-sheet-body'});
    // Notes on how the rule behaves beyond who, when and its numbers.
    const notes:HTMLElement[]=[];
    if(policy._conversion_id)notes.push(el('div',{class:'actions'},el('span',{class:'muted'},'Retained source · current destinations:'),
      ...(policy._destinations as Row[]).map(d=>d.kind==='policy'?button(catalogueEntryOf(find('policies',d.id)?.definition?.rule)?.name||'Current rule',()=>navigate(`rules/${d.catalogue_id}/${d.line_id}`),'link-button')
        :d.fact?.person_id?button('Person quota or offer',()=>navigate(routeHash('people',[],{open:d.fact.person_id,tab:'facts',fact:d.id}).slice(1)),'link-button')
          :d.fact?.calendar_entry_id?button('Calendar protection',()=>{const entry=find('programme_calendar_entries',d.fact.calendar_entry_id);if(entry)calendarProtectionPanel(ctx(),entry);},'link-button'):'')
      ,button('Department proposal',()=>simplifyPanel(ctx(),refresh,policy._conversion_id),'link-button')));
    if(rule.prorate_department_days)notes.push(note(`Default ${rule.amount} ${quotaUnits[rule.metric]||rule.metric} per ${rule.reference_days} days. The rota target scales to dated departmental membership and the days a person is not away, excluding external rotations. Whole sessions round ${rule.rounding}.`));
    if(rule.supervisor_ids?.length)notes.push(note('Allocation preference applies with these named supervisors: '+rule.supervisor_ids.map((id:string)=>name('people',id)).join(', ')+'. Clinical supervision authority is checked separately.'));
    if(rule.leave_buffer_days)notes.push(note(`Applies only within ${rule.leave_buffer_days} calendar days before or after approved or recorded leave, on the selected weekdays. Private leave reasons are not disclosed.`));
    if(rule.kind==='allocation_preference'&&rule.balance_by&&rule.balance_by!=='none')notes.push(el('p',{},`${rule.balance_by==='service_mode'?`Target ${Math.round(rule.balance_target_share*10000)/100}% ${rule.balance_target_mode} duties`:`Prefer fewer ${rule.balance_by==='supervisor'?'duties with the same supervisor':rule.balance_by==='supervisor_group'?'duties with any selected supervisor':'matching duties'}`} per ${rule.balance_window==='rolling'?`${rule.balance_window_days} local days`:rule.balance_window==='rota'?'planning period':'configured calendar week'}. Balancing weight: ${rule.balance_weight}.`));
    if(['maximum_participants','supervision_limit'].includes(rule.kind)&&rule.overflow_maximum)notes.push(note(`Up to ${rule.overflow_maximum} extra places${rule.kind==='supervision_limit'?' per supervisor':''} for ${rule.overflow_category_ids.map((id:string)=>name('categories',id)).join(', ')}. Normal places are attempted first; overflow is flagged for review.`));
    if(rule.kind==='minimum_cover'&&rule.when_person_ids?.length)notes.push(note('Required when any of these employees is assigned: '+rule.when_person_ids.map((id:string)=>name('people',id)).join(', ')+'. They do not count toward this minimum.'));
    // Balancing on an allocation preference is superseded by Balance goals: offer the same intent as a goal (the server
    // maps it, as Group similar rules does; activating the goal retires this preference).
    if(rule.kind==='allocation_preference'&&rule.balance_by&&rule.balance_by!=='none'&&policy.status==='active'&&may('rule.propose'))
      notes.push(el('p',{class:'hint'},'Balancing now lives in Balance goals, which also count history from published rotas. ',button('Convert to Balance goal…',()=>convertToGoal(policy),'secondary small')));
    if(rule.kind==='allocation_overtime'&&rule.required_policy_ids?.length){
      const off=rule.required_policy_ids.filter((id:string)=>!inForce.has(id));
      notes.push(el('div',{class:'notice '+(off.length&&policy.status==='active'?'warning':'info'),role:'note'},
        off.length?`Overtime is offered only while every limit it needs is in force. ${plural(off.length,'limit')} it needs ${off.length===1?'is':'are'} no longer in force, so it is switched off. `:`Needs these limits in force: ${rule.required_policy_ids.map((id:string)=>name('policies',id)).join(', ')}. `,
        off.length&&policy.status!=='retired'&&may('rule.propose')?button('Switch back on…',()=>fixOvertime([policy]),'secondary small'):''));
    }
    if(definition.workspace_id)notes.push(note('Applies to one rota only (added from that rota’s Rules & targets); other rotas are not affected.'));
    if(definition.calendar_entry_id)notes.push(note('Protected time from the academic calendar ('+name('programme_calendar_entries',definition.calendar_entry_id)+'). Change its days and hours from Programmes › Academic calendar.'));
    if(definition.overrides?.length)notes.push(note(`Replaces ${definition.overrides.map((id:string)=>name('policies',id)).join(', ')} for the people this rule matches; everyone else keeps ${definition.overrides.length===1?'that rule':'those rules'}.`));
    const replacedBy=rows('policies').filter(p=>p.status!=='retired'&&(p.definition?.overrides||[]).includes(policy.id));
    if(replacedBy.length)notes.push(note(`For some people this rule is replaced by ${replacedBy.map(p=>labelOf(p)+(p.status==='active'?'':' (waiting for approval)')).join(', ')}.${canLookupRules()?' Use “Which rule applies?” to see who.':''}`));
    const details=ruleDetails(rule,zone());
    panel.append(sheetSection('Who & when',edit(),whoWhen(scope,rule,name,!!definition.listed_only),...(details.length?[el('p',{class:'hint rl-details'},details.join(' '))]:[]),...notes));
    const limits=allLimits(definition);
    if(limits.length)panel.append(sheetSection('Numbers',edit(),numbersBlock(limits,perWords(rule))));
    const lines:Row[]=definition.per_person||[];
    if(LINE_KINDS.includes(rule.kind)&&(lines.length||canEdit)){
      if(plainLines(definition))panel.append(sheetSection('Lines for specific people or programmes',canEdit?compact(button('New line',()=>ruleChangeForm(ctx(),policy,-1),'ghost small'),'plus'):'',
        ...(lines.length?[exceptionList(rule,lines,name,zone(),canEdit?i=>ruleChangeForm(ctx(),policy,i):undefined)]:[]),
        el('p',{class:'hint'},!lines.length?'None: everyone uses the numbers above.':definition.listed_only?'The rule applies only to these.':'Everyone else uses the numbers above.')));
      else panel.append(linesSection(policy));
    }
    panel.append(draftHealth(policy));
    panel.append(ruleFacts([['Effective',ruleDatesNode(policy)],['Exception requests',allowsException(policy)?'Allowed with approval':'Not allowed'],
      ['Checked against',definition.workspace_id?'One rota only':'Rotas being planned'],...(rule.kind==='block_quota'?quotaFacts(rule).slice(1):[])]));
    const merged=(definition.merged_ids||[]).length;
    const foot=el('div',{class:'rl-sheet-foot'});
    if(permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER))foot.append(compact(button('Preview impact',async()=>{const result=await get('/api/policies/'+policy.id+'/impact');showDialog('What this rule changes','Rotas being planned that this rule would affect. Published rotas keep the rules they were approved under.',b=>{if(!result.impact.length)b.append(note('It changes nothing in the rotas being planned.'));for(const i of result.impact)b.append(el('h3',{},i.workspace_name+' · '+i.status),note(`${plural(i.findings.length,'issue')} from this rule. The full check below also lists open places and the rota's other issues.`),findingsBlock(i.evaluation));});},'secondary'),'chart'));
    if(review)foot.prepend(button('Review…',review,'primary'));
    if(canEdit)foot.append(compact(button('Change…',change,'ghost'),'edit'));
    foot.append(el('span',{class:'grow'}));
    if(policy.status==='active'&&may('policy.retire'))foot.append(compact(button('Retire rule',()=>confirmAction('Retire rule',
      `${safetyRetirement(policy)?SAFETY_RETIREMENT_NOTICE+' ':''}It stops applying to rotas being planned.${merged?` The ${plural(merged,'rule')} it replaced apply again, as they were approved.`:''} Published rotas keep the rules they were approved under.`,
      async reason=>{const r=await command<{restored_ids?:string[];approval?:Row}>('policy.retire',{reason},policy.id,policy.revision);close();
        if(notifyWaiting([r],me?.identity_id,refresh)){await refresh();return;}
        // Said before the refresh, so its message carries the retirement's Undo (api.ts UNDO).
        notify(r.restored_ids?.length?`Rule retired. The ${plural(r.restored_ids.length,'rule')} it replaced apply again.`:'Rule retired. It no longer applies to rotas being planned.');await refresh();},'Retire rule'),'ghost'),'trash'));
    else if(policy.status==='active'&&permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR))foot.append(el('span',{class:'hint'},'An approver retires rules.'));
    // A rule that was in force (not a declined proposal) can be restored; the server refuses when another rule replaced it.
    if(policy.status==='retired'&&policy.approved_by&&!policy._conversion_id&&may('policy.restore'))foot.append(compact(button('Restore…',()=>confirmAction('Restore rule',
      'It applies again as it was approved, to rotas being planned. Published rotas keep the rules they were approved under.',
      async reason=>{const r=await command<{record:Row;approval?:Row}>('policy.restore',{reason},policy.id,policy.revision);close();await refresh();
        if(notifyWaiting([r],me?.identity_id,refresh))return;
        // Undo retires it again; a grouped rule is retired without bringing its old rules back (they were not restored).
        notify('Rule restored. It applies again to rotas being planned.','success',{label:'Undo',run:async()=>{
          const u=await command('policy.retire',{reason:definition.merged_ids?.length?`${ENDED_REASON}: restored by mistake (Undo)`:'Restored by mistake (Undo)'},r.record.id,r.record.revision);takeUndo();  // this is the Undo: no Undo of it
          if(!notifyWaiting([u],me?.identity_id,refresh))notify('Rule retired again.','info');await refresh();}});},'Restore rule'),'secondary'),'history'));
    body.append(rulePanels(policy.id,panel,()=>historySection(policy),()=>ruleFacts([['Policy owner',policy.owner||'Not recorded'],['Source or decision',policy.source||'Not recorded']])),foot);
  });
}
/** A rule's row in the Rules list (the open rule's outline, and its place for ↑ ↓). */
const ruleMark=(ids:string)=>`.rl-row[data-rule-ids="${CSS.escape(ids)}"]`;
/** A rule's docked side panel (beside the Rules list, or the Schedule's Why): its heading line says classification,
 *  state and the rule's own name; the rule's row is outlined in the list. */
function rulePanel(family:Row[],title:string,ruleName:string,position:PanelPosition|undefined,build:(d:Dock)=>void){
  const policy=family[0],d=policy.definition,state=family.length>1?plural(family.length,'copy','copies'):policy.status==='active'?`Effective from ${formatDate(policy.starts_on)}`:ruleState(policy);
  const ids=family.map(p=>p.id).join(' ');
  dockPanel({key:'rule',className:'rule-sheet',title,width:600,position,mark:ruleMark(ids),
    sub:el('span',{class:'rl-meta'},levelBadge((policy.effective_severity??d.severity)==='mandatory'?'mandatory':d.strength||'medium',policy.severity_reason),el('span',{},state),el('span',{'aria-hidden':'true'},'·'),el('span',{},ruleName)),
    build:dock=>{dock.panel.dataset.ruleIds=ids;build(dock);}});
}
/** Close the rule panel when it shows one of these rules (a newer proposal replaced it, or the reader left for the Schedule). */
const closeRulePanel=(id?:string)=>{if(document.querySelector(id?`.dock-panel.rule-sheet[data-rule-ids~="${CSS.escape(id)}"]`:'.dock-panel.rule-sheet'))closeDock();};
/** A rule's lines as a compact table: only the columns some line uses. Big rules get the list view (search, paging). */
type LineCol=[string,(l:Row)=>string,((l:Row)=>number)?,((l:Row)=>Node)?];
function linesTable(policy:Row):HTMLElement{
  const d=policy.definition,order=isOrder(d),key=lineField(d.rule.kind,d);
  const dates=(l:Row)=>l.starts_on||l.ends_on?[l.starts_on?formatDate(l.starts_on):'Rule start','– '+(l.ends_on?formatDate(lastIncludedDate(l.ends_on)):'rule end')]:['Rule’s dates'];
  // Rows keep their place (_i): an allocation order reads first to last.
  const lines:Row[]=(order?orderedLines(d):d.per_person||[]).map((l:Row,i:number)=>({...l,_i:i}));
  if(d.rule.kind==='allocation_overtime')return overtimeTable(policy);
  const used=(f:(l:Row)=>unknown)=>lines.some(l=>{const v=f(l);return Array.isArray(v)?v.length>0:!!v;});
  const names=(kind:string,ids:string[]|undefined)=>(ids||[]).map(id=>name(kind,id)).join(', ');
  const who=lineWho(d.rule.kind);
  // The same words as the line editor for a line that names no one; an order that names no one has no Who column.
  const anyone=who==='Who counts'?'Anyone it matches':who==='Supervisor'?'Any supervisor':d.listed_only?'Anyone':'Everyone it matches';
  // A line's own settings, for the settings some line sets (a quota's maximum and target also when the rule has one).
  const own=(d.listed_only||ANY_LINE_SETTINGS.includes(d.rule.kind)?(LINE_SETTINGS[d.rule.kind]||[]):[]).filter(k=>used(l=>l.settings?.[k]??null)||d.rule.kind==='block_quota'&&d.rule[k]!=null)
    .map(k=>[SETTING_LABELS[k]||k,(l:Row)=>{
      const v=l.settings?.[k],set=v!=null&&!(Array.isArray(v)&&!v.length);
      const text=settingWords(k,set?v:d.rule[k],Number(l.amount??d.rule.maximum));return set||text==='—'?text:`${text} (rule)`;},k] as [string,(l:Row)=>string,string]);
  const quotaNumbers=own.filter(c=>['maximum_amount','target_amount'].includes(c[2])).map(([label,f]):LineCol=>[label,f]),settings=own.filter(c=>!['maximum_amount','target_amount'].includes(c[2])).map(([label,f]):LineCol=>[label,f]);
  const cols:LineCol[]=[
    ...(order?[['#',(l:Row)=>String(l._i+1),(l:Row)=>l._i] as LineCol]:[]),
    ...(!order||used(l=>l.person_id||l.programme_id)?[[who==='Person'?'Who':who,(l:Row)=>[l.person_id?name('people',l.person_id):'',l.programme_id?name('programmes',l.programme_id):''].filter(Boolean).join(' · ')||anyone] as LineCol]:[]),
    ...(key&&!order&&lineHasNumber(d.rule)?[[d.rule.kind==='block_quota'?quotaNumberLabel(d.rule):lineLabel(d.rule.kind,key),(l:Row)=>!lineHasNumber(d.rule,l)?'—':l.amount==null?`${d.rule[key]} (rule)`:String(l.amount),(l:Row)=>Number(l.amount??d.rule[key])] as LineCol]:[]),
    ...quotaNumbers,
    ...(used(l=>l.starts_on||l.ends_on)?[['Dates',(l:Row)=>dates(l).join(' '),undefined,(l:Row)=>dateUnits(dates(l))] as LineCol]:[]),
    // Columns in the line editor's order: ranks, days, hours, clinics, duty types.
    ...(used(l=>l.category_ids)?[['Ranks',(l:Row)=>names('categories',l.category_ids)||'Any'] as LineCol]:[]),
    ...(used(l=>l.weekdays)?[['Days',(l:Row)=>daysOf(l)] as LineCol]:[]),
    ...(used(l=>l.start_time)?[['Hours',(l:Row)=>l.start_time?`${l.start_time}–${l.end_time}`:'All day'] as LineCol]:[]),
    ...(used(l=>l.service_ids)?[['Clinics',(l:Row)=>names('services',l.service_ids)||'Any'] as LineCol]:[]),
    ...(used(l=>l.assignment_type_ids)?[['Duty types',(l:Row)=>names('assignment_types',l.assignment_type_ids)||'Any'] as LineCol]:[]),
    ...settings];
  return lineRows(lines,cols);
}
/** A line setting in words (people, ranks, the kind of supervision limit, a number); empty values say what they mean. */
/** Why a run's fair share search stopped (solver_control `stop_reason`), in words. */
const FAIR_STOP_WORDS:Record<string,string>={optimal:'the fairest result was found',plateau:'no further progress',time_limit:'the search time ran out',
  slice_limit:'the search time ran out',operator_stop:'stopped by a person',not_reached:'not reached in the search time'};
function settingWords(key:string,value:unknown,number:number):string{
  const ids=Array.isArray(value)?value as string[]:[];
  if(key==='mode')return value==='allowed_ranks'?MODE_LABELS.allowed_ranks:Number.isFinite(number)?`Up to ${number}`:'Up to the number';
  if(key==='overflow_maximum')return String(value??0);
  if(key==='maximum_amount'||key==='target_amount')return value==null?'—':String(value);
  if(key.endsWith('category_ids'))return ids.map(id=>name('categories',id)).join(', ')||(key==='trainee_category_ids'?'All trainee ranks':'None');
  return ids.map(id=>name('people',id)).join(', ')||(key==='when_person_ids'?'Always':'Any supervisor');
}
/** Rows as a compact table, or (over 25) the list view with search and paging. */
function lineRows(lines:Row[],cols:LineCol[]):HTMLElement{
  if(lines.length<=25){
    const view=table(cols.map(c=>c[0]),lines.map(l=>cols.map(c=>(c[3]||c[1])(l))));
    view.querySelector('table')!.setAttribute('role','table');
    view.querySelectorAll('tr').forEach(row=>row.setAttribute('role','row'));
    view.querySelectorAll('th').forEach(cell=>cell.setAttribute('role','columnheader'));
    view.querySelectorAll('tbody tr').forEach(row=>row.querySelectorAll('td').forEach((cell,i)=>{
      cell.setAttribute('role','cell');cell.prepend(el('span',{class:'rule-cell-label','aria-hidden':'true'},cols[i][0]));
    }));
    return el('div',{class:'rule-lines-table'},view);
  }
  return dataTable<Row>({id:'rule-line-rows',noun:['line','lines'],rows:lines,rowId:l=>String(l._i),pageSize:25,searchLabel:'Search lines',searchPlaceholder:'Person, programme or clinic',
    columns:cols.map(([label,f,num,render],i)=>({key:'c'+i,label,num:!!num,text:f,...(num?{sort:num}:{}),
      ...(['Clinics','Duty types','Days','Ranks'].includes(label)?{facet:(l:Row)=>f(l).split(', ')}:{}),cell:(l:Row)=>render?render(l):num?el('span',{class:'mono'},f(l)):f(l)}))}).root;
}
/** Overtime offers read one row per person: "Tue: Evening shift · Fri, Sat: Day shift", and their overtime cap from the
 *  active caps the offer needs (an overtime-only maximum quota: the person's own line, or the cap's number). */
function overtimeTable(policy:Row):HTMLElement{
  const d=policy.definition,need:string[]=d.rule.required_policy_ids||[];
  const caps=rows('policies').filter(p=>isCap(p)&&p.definition.rule.kind==='block_quota'&&p.definition.rule.overtime==='only'
    &&(need.includes(p.id)||(p.definition.merged_ids||[]).some((id:string)=>need.includes(id))));
  const capFor=(pid:string)=>[...new Set(caps.flatMap(c=>{const own=(c.definition.per_person||[]).filter((l:Row)=>l.person_id===pid);
    const maximum=c.definition.rule.comparison==='range'?c.definition.rule.maximum_amount:c.definition.rule.amount;
    return (own.length?own.map((l:Row)=>l.amount??maximum):c.definition.listed_only?[]:[maximum]).map((a:unknown)=>quotaAmount(c.definition.rule,a));}))].join(' · ')||'No cap';
  const people=new Map<string,Row[]>();
  for(const l of d.per_person||[]){const k=l.person_id||'programme:'+(l.programme_id||'');people.set(k,[...(people.get(k)||[]),l]);}
  // shortcut: a line's own dates are not shown here; the editor (Edit lines) has them.
  const offers=(ls:Row[])=>{const byWhat=new Map<string,Set<number>|null>();
    for(const l of ls){const what=lineSessions({service_ids:l.service_ids,assignment_type_ids:l.assignment_type_ids},name)+(l.start_time?` ${l.start_time}–${l.end_time}`:'');
      const had=byWhat.get(what);byWhat.set(what,!l.weekdays||had===null?null:new Set([...(had||[]),...l.weekdays]));}
    return [...byWhat].map(([what,days])=>`${days?weekdayNames([...days],true):'Every day'}: ${what==='Any session'?'any duty':what}`).join(' · ');};
  const list=[...people.values()].map((ls,i)=>({_i:i,pid:ls[0].person_id||'',who:ls[0].person_id?name('people',ls[0].person_id):ls[0].programme_id?name('programmes',ls[0].programme_id):'Anyone',offers:offers(ls)}) as unknown as Row)
    .sort((a,b)=>String(a.who).localeCompare(String(b.who)));
  return lineRows(list,[['Who',r=>r.who],['Overtime offered',r=>r.offers],['Overtime cap',r=>r.pid?capFor(r.pid):'—']]);
}
/** A rule's lines, and (coordinators, admins) editing them in place: every edit is one proposed new version. */
function linesSection(policy:Row):HTMLElement{
  const d=policy.definition,rule=d.rule,lines:Row[]=d.per_person||[],key=lineField(rule.kind,d),order=isOrder(d);
  const canEdit=policy.status!=='retired'&&may('rule.propose');
  const box=el('section',{class:'rule-lines','aria-label':'Lines of '+labelOf(policy)});
  box.hidden=!lines.length&&!canEdit; // nothing to show or do
  const title=!lines.length?'Lines':order?`Fills ${plural(lines.length,'line')} in order`:rule.kind==='allocation_overtime'?`Offered to ${plural(new Set(lines.map(l=>l.person_id||l.programme_id)).size,'person','people')} · ${plural(lines.length,'line')}`
    :d.listed_only?`${plural(lines.length,'line')} · the rule applies only to these`:`${plural(lines.length,'line')} with their own numbers`;
  const view=()=>box.replaceChildren(el('div',{class:'rule-lines-head'},el('h4',{},title),canEdit?compact(button(lines.length?(order?'Edit order':'Edit lines'):'Add lines',edit,'secondary small'),'edit'):''),
    lines.length?linesTable(policy):el('p',{class:'muted'},'Give a person, programme or clinic its own number or dates.'),
    lines.length?el('p',{class:'hint'},order?'Auto-schedule fills the first line’s sessions first, then the next.'
      :rule.kind==='allocation_overtime'?'Auto-schedule offers overtime on these days and duties, after regular candidates, within each person’s cap.':d.listed_only?'People and duties outside these rows are not covered by this rule. A row without a value uses the rule’s value.'
      :rule.kind==='block_quota'?'A listed person follows their own lines (each is their block); everyone else the rule matches gets the rule’s numbers.':'A listed person gets their own number on their line’s dates; on other days, and for everyone else, the rule’s number applies.'):'');
  const edit=()=>{
    const ed=perPersonEditor(ctx(),rule.kind,lines as PersonAmount[],{listed:!!d.listed_only,base:key?rule[key]:null,lineNumber:d.line_number,rule:()=>rule});
    const save=button('Propose change',async()=>{
      const bad=ed.wrap.querySelector<HTMLInputElement>('input:invalid,select:invalid');if(bad){bad.reportValidity();return;}
      const next=ed.read();if(!next.length&&d.listed_only)throw new Error('Keep at least one line. To stop the rule applying, retire it.');
      // Nothing to approve when nothing changed (lines compared without empty fields; an order first to last).
      const plain=(ls:Record<string,any>[])=>JSON.stringify(ls.map(l=>Object.entries(l).filter(([,v])=>v!=null&&!(Array.isArray(v)&&!v.length)).sort()));
      if(plain(next)===plain(order?orderedLines(d):lines)&&ed.listed()===!!d.listed_only){notify('No changes to propose.','info');view();return;}
      await proposeWithUndo(policy,def=>{def.per_person=next;def.listed_only=ed.listed();});
      view();  // the rule in force is unchanged until an approver activates the proposal
    },'primary small');
    box.replaceChildren(ed.wrap,el('div',{class:'actions rule-lines-save'},save,armed(button('Discard changes',view,'ghost small'),'Discard?'),
      el('span',{class:'muted'},'Proposed as a new version. The current one applies until an approver activates it.')));
  };
  view();return box;
}
/** Earlier versions of a rule (the rule it replaced, the rules grouped into it, and theirs) and what replaced it. */
function historySection(policy:Row):HTMLElement{
  const out=el('div',{class:'rule-history-body'});
  const box=el('section',{class:'rule-history'},el('h4',{},'Version history'),out);
  const load=()=>{out.replaceChildren(skeleton('lines','Loading earlier versions…'));
    loadRetired().then(retired=>{
      if(!box.isConnected)return;
      const all=[...rows('policies'),...retired],byId=new Map(all.map(p=>[p.id,p])),seen=new Set<string>([policy.id]),earlier:Row[]=[];
      const walk=(p:Row)=>{for(const id of [p.definition?.replaces_id,...(p.definition?.merged_ids||[])].filter(Boolean) as string[]){if(seen.has(id))continue;seen.add(id);const q=byId.get(id);if(q){earlier.push(q);walk(q);}}};
      walk(policy);
      const later=all.filter(q=>q.id!==policy.id&&(q.definition?.replaces_id===policy.id||(q.definition?.merged_ids||[]).includes(policy.id)));
      void swap(out,...later.map(q=>note(`${q.status==='proposed'?'Would be replaced':'Replaced'} by ${labelOf(q)} · ${ruleState(q).toLowerCase()}.`)),
        earlier.length?table(['Earlier rule','What it asked','Dates','Status'],earlier.map(q=>[labelOf(q),ruleSentence(q.definition.rule,q.definition),ruleDatesNode(q),badge(ruleState(q),q.status==='active'?'ok':'neutral')]))
          :emptyLine('No earlier versions. This is the first version of this rule.'));
    }).catch(e=>{if(box.isConnected)out.replaceChildren(note((e as Error).message,'error',e),button('Retry loading history',load,'secondary small'));});};
  load();
  return box;
}
/** Propose a changed rule, then offer Undo: withdraws the proposal (api.ts UNDO). A corrected proposal also declined
 *  the one it corrects, so its Undo proposes that one again. */
async function proposeWithUndo(policy:Row,change:(definition:Row)=>void,dates?:{starts_on:string;ends_on:string|null}){
  const previous=policy.status==='proposed'?policy:null;
  const created=await proposeLine(policy,change,dates);
  // A corrected proposal replaces the one an open sheet shows: that sheet closes (the list shows the new one).
  if(previous)closeRulePanel(previous.id);
  // The message (and its Undo) first: the refresh after it can take a while on a big rule set.
  notify('Change proposed. The current rule applies until an approver activates it.','success',previous?{label:'Undo',run:async()=>{
    // Proposed again first: the decline after it ends the undo batch, so this Undo offers no Undo of its own.
    await command('policy.create',{name:previous.name,owner:previous.owner,source:previous.source,starts_on:previous.starts_on,ends_on:previous.ends_on,definition:previous.definition});
    await command('policy.decline',{reason:WITHDRAWN},created.record.id,created.record.revision);
    await refresh();notify('Change withdrawn.','info');}}:undefined);
  await refresh();
}
/** Overtime offers switched off because the limits they need were retired: point them at the limits in force now.
 *  A retired limit maps to its new version, or to the one limit in force of the same rule. */
async function fixOvertime(list:Row[]){
  const preview=await get<{groups:Row[];changes:Row[]}>('/api/policies/groups');
  const found=list.map(p=>({p,c:preview.changes.find(c=>c.shape==='overtime'&&c.rule_ids[0]===p.id)}));
  const ready=found.filter(x=>x.c),stuck=found.filter(x=>!x.c);
  // Why the server has no switch-on for an offer, in plain words.
  const why=(p:Row)=>preview.groups.some(g=>g.payload&&g.rule_ids.includes(p.id))
    ?`${labelOf(p)} can become one rule with similar offers first: use Group similar rules, then switch the grouped rule back on.`
    :`${labelOf(p)} can’t be switched back on yet: no rule in force checks as much as the retired limits it needs. Propose those limits again first.`;
  if(!ready.length){notify(list.length===1?why(list[0]):`None of these ${list.length} offers can be switched back on yet: no rule in force checks as much as the retired limits they need.`,'info');return;}
  const intro=el('div',{class:'change-list span-2'},...ready.map(({p,c})=>changeSummary(ready.length>1?labelOf(p):'',c!)),
    ...(stuck.length?[note(stuck.length===1?why(stuck[0].p):`${plural(stuck.length,'offer')} can’t be switched back on yet: no rule in force checks as much as the retired limits they need.`,'warning')]:[]));
  confirmAction(ready.length===1?'Switch overtime back on':`Switch ${plural(ready.length,'overtime offer')} back on`,
    'Auto-schedule offers overtime again, within the limits below. Proposed as a new version: nothing changes until an approver activates it, and retiring it switches the offer off again.',
    reason=>proposeChanges({changes:ready.map(x=>[x.p.id]),reason},ready.length===1?'Switch-on proposed. The offer stays off until an approver activates it.':`${ready.length} switch-ons proposed. Each offer stays off until an approver activates it.`),
    ready.length===1?'Propose switch-on':`Propose ${ready.length} switch-ons`,intro);
}
/** One previewed change that is not identical, in words: what it did, what it will do, and the limits it keeps to. */
function changeSummary(title:string,c:Row):HTMLElement{
  return el('div',{class:'change-summary'},title?el('strong',{},title):'',
    el('p',{},el('span',{class:'change-label'},'Was'),String(c.was||'')),el('p',{},el('span',{class:'change-label'},'Now'),String(c.now||'')),
    ...(c.limits?.length?[el('p',{class:'muted'},`Limits: ${c.limits.join(', ')}`)]:[]));
}
/** Propose previewed changes and groups (policy.group); the message offers Undo, which withdraws each new proposal
 *  (api.ts UNDO; not when calendar entries were added on the way). */
async function proposeChanges(payload:Record<string,unknown>,message?:string){
  // The message (and its Undo) first: the refresh after it can take a while on a big rule set.
  const result=await command<{message:string;records:Row[];calendar_entries?:number}>('policy.group',payload);
  notify(message||result.message);await refresh();
}
/** An allocation balance as a Balance goal, as the server maps it: was and now, then propose in one step or edit first. */
async function convertToGoal(policy:Row){
  const preview=await get<{groups:Row[];changes:Row[]}>('/api/policies/groups');
  const change=preview.changes.find(c=>c.shape==='goal'&&c.rule_ids[0]===policy.id);
  if(!change){notify(preview.groups.some(g=>g.payload&&g.rule_ids.includes(policy.id))
    ?'This preference can become one rule with similar preferences first: use Group similar rules, then convert the grouped rule.'
    :'This preference can’t become a Balance goal as it is: it also sets a priority, supervisors, hours or places, or its days aren’t a weekend. Propose a Balance goal with Propose rule instead.','info');return;}
  const payload=change.payload,edit=button('Edit before proposing…',()=>{
    edit.closest('dialog')?.close();
    return policyForm(ctx(),'',{kind:'balance_goal',rule:payload.definition.rule,title:payload.name,starts_on:payload.starts_on,ends_on:payload.ends_on||undefined,
      conditions:scopeAsConditions(payload.definition.scope||{}),merged_ids:payload.definition.merged_ids});},'ghost small');
  confirmAction('Convert to Balance goal','Auto-schedule then aims for a fair share instead. Proposed as a new rule: nothing changes until an approver activates it, which retires this preference; retiring the goal brings it back.',
    reason=>proposeChanges({changes:[[policy.id]],reason},'Balance goal proposed. The preference applies until an approver activates the goal.'),'Propose Balance goal',
    el('div',{class:'change-list span-2'},changeSummary('',change),el('div',{class:'actions'},edit)));
}
/** The lines of an older rule family (copies that differ by who, when or the number): select to activate, decline or retire. */
function ruleLinesTable(lines:Row[],key:string|undefined,bulk:(picked:Row[])=>BulkAction[]){
  const who=(r:Row)=>{const s=r.definition.scope;return [...personIds(s).map(id=>name('people',id)),...(s.tags||[]).map((t:string)=>'Tag: '+t),...(s.capability_ids||[]).map((id:string)=>name('capabilities',id)),
    ...[...(s.category_ids||[]),...(s.category_id?[s.category_id]:[])].map((id:string)=>name('categories',id)),...(s.programme_id?[name('programmes',s.programme_id)]:[]),...(conditionWords(s)?[conditionWords(s)]:[])];};
  const stage=(r:Row):[string,Tone]=>[ruleState(r),r.status==='proposed'?'warn':r.status==='active'?'ok':'neutral'];
  // What every line shares (e.g. the clinics) is said once above the table; the rows show only what differs.
  const keys=[...new Set(lines.flatMap(r=>Object.keys(r.definition.scope||{})))];
  const same=keys.filter(k=>k!=='weekdays'&&lines.every(r=>JSON.stringify(r.definition.scope?.[k]??null)===JSON.stringify(lines[0].definition.scope?.[k]??null)));
  const own=(r:Row)=><Row>Object.fromEntries(Object.entries(r.definition.scope||{}).filter(([k])=>!same.includes(k)));
  const shared=scopeChips(<Row>Object.fromEntries(same.map(k=>[k,lines[0].definition.scope[k]]))).filter(c=>c.textContent!=='Everyone · all duties');
  const table=dataTable<Row>({id:'rule-lines',noun:['line','lines'],rows:lines,rowId:r=>r.id,pageSize:25,
    searchLabel:'Search lines',searchPlaceholder:'Person, tag, skill or rank',
    columns:[
      {key:'who',label:'Applies to',text:r=>who(r).join(' '),facet:r=>who(r).length?who(r):['Everyone'],cell:r=>pillList(scopeChips(own(r)),{label:'scope'})},
      {key:'dates',label:'Dates',sort:r=>String(r.starts_on),cell:r=>ruleDatesNode(r)},
      {key:'days',label:'Days',facet:r=>daysOf(r.definition.scope),cell:r=>daysOf(r.definition.scope)},
      ...(key?[{key:'value',label:VALUE_LABEL[lines[0].definition.rule.kind]||'Value',num:true,sort:(r:Row)=>Number(r.definition.rule[key]),facet:(r:Row)=>String(r.definition.rule[key]),cell:(r:Row)=>el('span',{class:'mono'},String(r.definition.rule[key]))}]:[]),
      {key:'status',label:'Status',facet:r=>stage(r)[0],cell:r=>badge(...stage(r))},
    ],
    bulk:ids=>bulk(lines.filter(r=>ids.includes(r.id))),
  }).root;
  return shared.length?el('div',{},el('div',{class:'rule-scope rule-shared'},el('span',{class:'muted'},'Every line:'),...shared),table):table;
}
/** Propose a new version of a line (it replaces the line once approved); a line still waiting is withdrawn instead of kept twice. */
async function proposeLine(line:Row,change:(definition:Row)=>void,dates?:{starts_on:string;ends_on:string|null},title=line.name){
  const definition=structuredClone(line.definition);change(definition);
  definition.replaces_id=line.status==='active'?line.id:line.definition.replaces_id||null;
  const created=await command('policy.create',{name:title,owner:line.owner,source:line.source,starts_on:dates?.starts_on||line.starts_on,ends_on:dates?dates.ends_on:line.ends_on,definition});
  if(line.status==='proposed'&&line.id)await command('policy.decline',{reason:'Replaced by a corrected proposal'},line.id,line.revision);
  return created;
}
/** Change a rule in four steps (the form stepper, dom.ts): Who & when (conditions and dates), Numbers, Exceptions (a
 *  person's or a programme's own numbers and dates; rules whose lines name clinics, ranks or hours keep the full line
 *  editor), then Review & propose. Proposed as a new version that replaces this one once a different person approves
 *  it; published rotas keep the rules they were approved under. `at`: open on Exceptions, with that line open (-1: a
 *  new person). */
/** `prefill`: a number to propose instead (Make it the department default, from a rota's change). */
function ruleChangeForm(c:Context,policy:Row,at?:number,prefill?:number){
  const d=policy.definition,rule=d.rule,quota=rule.kind==='block_quota',key=VALUE_KEY[rule.kind];
  const {dialog}=formDialog('Change '+labelOf(policy),'Proposed as a new version; once approved it replaces this one. Published rotas keep the rules they were approved under.',form=>{
    form.dataset.reviewTitle='Review & propose';
    const who=conditionBuilder(ruleConditionFields(c),scopeAsConditions(d.scope||{}),{context:'rule'});
    // A quota's minimum, maximum and target in one place; other rules have one number.
    const limits=quota?quotaLimits(rule):null;
    const value=key&&!quota?field(VALUE_LABEL[rule.kind]||'Value','number',prefill??rule[key],{required:true,step:'any',min:rule.kind==='allocation_preference'?-100:0}):null;
    const from=field('From','date',policy.starts_on,{required:true}),until=field('Until (included)','date',policy.ends_on||'',{required:quota});lastDayIncluded(until.input);
    const ruleNow=()=>limits?{...rule,...limits.read()}:value?{...rule,[key!]:Number(value.input.value)}:rule;
    const plain=LINE_KINDS.includes(rule.kind)&&plainLines(d);
    const lines=!LINE_KINDS.includes(rule.kind)?null:plain?exceptionsEditor(ruleNow,d.per_person||[],{people:peopleChoices(c),programmes:rows('programmes').map(p=>({value:p.id,label:labelOf(p)}))},name,zone(),!!d.listed_only)
      :perPersonEditor(c,rule.kind,d.per_person||[],{listed:!!d.listed_only,base:key?rule[key]:null,lineNumber:d.line_number,rule:ruleNow});
    if(limits&&lines&&'redraw' in lines)limits.wrap.addEventListener('input',()=>lines.redraw());
    const step=(name:string,node:HTMLElement)=>{node.dataset.step=name;return node;};
    form.append(step('Who & when',el('div',{class:'span-2 audience-conditions'},el('p',{class:'hint'},'Who and which sessions. People are checked at the time of each duty.'),who.root)),
      step('Who & when',dateRange('Dates',from,until)),
      ...(limits?[step('Numbers',limits.wrap)]:value?[step('Numbers',value.wrap)]:[]),...(lines?[step('Lines',lines.wrap)]:[]),
      step('Review',el('p',{class:'hint span-2'},'Proposed as a new version: an approver (a different person) activates it, and until then the current rule applies. The message after proposing offers Undo.')));
    if(at!==undefined&&lines&&'edit' in lines)at<0?lines.add():lines.edit(at);
    return async()=>{
      const next=lines?.read()||[];if(d.listed_only&&lines&&!next.length)throw new Error('Keep at least one exception. To stop the rule applying, retire it.');
      await proposeWithUndo(policy,def=>{
        // The older flat fields are read into the conditions above and saved there.
        def.scope={weekdays:def.scope?.weekdays,start_time:def.scope?.start_time,end_time:def.scope?.end_time,conditions:who.read()};
        if(key&&value)def.rule[key]=Number(value.input.value);
        if(limits){Object.assign(def.rule,limits.read());if(def.rule.comparison==='target'){def.severity='advisory';def.exception_allowed=false;}}
        if(quota){def.rule.starts_on=from.input.value;def.rule.ends_on=until.input.value;}
        if(lines){def.per_person=next;def.listed_only='listed' in lines?lines.listed():!!d.listed_only;}
      },{starts_on:from.input.value,ends_on:until.input.value||null});
    };
  },'Propose change');
  // Opened from an exception: start on the Exceptions step (each earlier step is checked on the way, as Next does).
  if(at!==undefined){[...dialog.querySelectorAll<HTMLButtonElement>('.stepper .step-link')].find(b=>b.textContent?.includes('Lines'))?.click();
    dialog.querySelector('.rl-exc.open,.pp-editor')?.scrollIntoView({block:'center'});}  // all on one page: scrolled to
}
/** Similar rules (the same rule for different people, programmes, clinics or back-to-back dates) become one rule with
 *  lines each. Previewed first; nothing applies until an approver activates each new rule, and retiring it brings the
 *  old rules back. Protected academic time is linked to the academic calendar at the same time. */
async function groupRulesDialog(focus:string[]=[]){
  const preview=await get<{groups:Row[];changes:Row[];rules:number;active:number;after:number}>('/api/policies/groups');
  const groups=preview.groups.filter(g=>g.payload),problems=preview.groups.filter(g=>!g.payload);
  // Ended rules are never proposed: an approver retires them (with the ended reason). The rest are proposed changes.
  // Ended rules and exact copies of another rule are never proposed: an approver retires them. The rest are proposed changes.
  const ended:Row[]=(preview.changes||[]).filter(c=>c.retire),changes:Row[]=(preview.changes||[]).filter(c=>!c.retire);
  const why=(g:Row)=>String(g.problem).replace(/^.*Value error, /s,'');
  if(!groups.length&&!changes.length&&!ended.length){notify(problems.length?`Similar rules found, but they cannot become one rule yet: ${problems[0].name} (${why(problems[0])}).`:'No similar rules to group. Each rule already says something different.','info');return;}
  const shapeWords:Record<string,string>={series:'back-to-back periods become one',places:'clinics become lines',protect:'protected time',
    range:'an at-least and an at-most become one range',supervision:'one table of supervisors',companions:'a line per person on duty',order:'joins the allocation order'};
  const changeTitles:Record<string,string>={goal:'Balance goal',overtime:'Switch overtime back on',companions:'Cover while someone is on duty',order:'Join the allocation order',ceiling:'Join the weekly ceiling'};
  const canRetire=may('policy.retire'),canGroup=may('rule.propose');
  /** Ended rules, and copies of another rule (one row per rule kept, retiring all its copies), each row with its own
   *  Retire (approvers; acts at once, D42) and Undo in the row (a toast would sit behind this dialog), or a note that
   *  an approver retires them. */
  const retireSection=(rowsOf:Row[][],title:string,why:string,head:(chs:Row[])=>string,summary:(chs:Row[])=>Row,prefix:(ch:Row)=>string,reasonOf:()=>string)=>el('section',{class:'span-2 group-changes group-ended','aria-label':title},el('h3',{},`${title} · ${rowsOf.reduce((n,r)=>n+r.length,0)}`),
    el('p',{class:'hint'},canRetire?`${why} Retire each one here; Restore in History brings it back.`:`${why} An approver retires them; they are not proposed.`),
    el('div',{class:'group-list'},...rowsOf.map(chs=>{
      const name=head(chs),cell=el('span',{class:'row-buttons'});
      let current=chs.map(ch=>rows('policies').find(p=>p.id===ch.rule_ids[0])).filter(Boolean) as Row[];
      let replies:Row[]=[];
      // Each rule's reply; what still waits for other approvers (nothing changed yet), or ''.
      const each=async(kind:string,reason:(i:number)=>string)=>{const next:Row[]=[];replies=[];
        for(const [i,p] of current.entries()){const r=await command<{record:Row;approval?:Row}>(kind,{reason:reason(i)},p.id,p.revision);next.push(r.record);replies.push(r as unknown as Row);}current=next;
        takeUndo();  // the row's own Undo restores them: no second one in a message
        return approvalPending(awaitingApproval(replies)[0]?.approval);};
      // Waiting for more approvers: the rules stay as they were (`retired`); Undo withdraws the approvals just given.
      const wait=(waiting:string,retired:boolean)=>{cell.replaceChildren(badge('Waiting for approval','warn'),el('small',{class:'hint'},waiting),
          button('Undo',async()=>{await withdrawApprovals(replies);paint(retired);announce(`${name}: approval withdrawn.`);},'ghost small',{'aria-label':`Withdraw approval for ${name}`}));
        announce(`${name}: ${waiting}.`);};
      const paint=(retired:boolean)=>cell.replaceChildren(...(retired?[badge('Retired','neutral'),button('Undo',async()=>{
          const waiting=await each('policy.restore',()=>'Undo');if(waiting){wait(waiting,true);return;}
          paint(false);announce(`${name}: restored.`);await refresh();},'ghost small',{'aria-label':`Undo retiring ${name}`})]
        :[button('Retire',async()=>{
          const waiting=await each('policy.retire',i=>`${prefix(chs[i])}: ${reasonOf()}`.slice(0,500));if(waiting){wait(waiting,false);return;}
          paint(true);announce(`${name}: retired.`);await refresh();},'secondary small',{'aria-label':`Retire ${name}`})]));
      if(canRetire&&current.length===chs.length)paint(false);
      return el('div',{class:'group-row change-row'},el('div',{class:'group-ended-head'},el('strong',{},name),cell),changeSummary('',summary(chs)));})));
  const copies=ended.filter(c=>c.shape==='duplicate'),past=ended.filter(c=>c.shape!=='duplicate');
  const byKept=[...copies.reduce((m,c)=>m.set(c.kept_id,[...(m.get(c.kept_id)||[]),c]),new Map<string,Row[]>()).values()];
  const retireSections=(reasonOf:()=>string)=>[
    ...(byKept.length?[retireSection(byKept,'Duplicate rules','Exact copies of another rule in force: every break was reported once per copy.',
      chs=>chs.length===1?String(chs[0].name):`${chs.length} copies of ${chs[0].kept_name}`,
      chs=>chs.length===1?chs[0]:{...chs[0],was:`${chs.length+1} rules say exactly the same (${chs.slice(0,3).map(c=>c.name).join(', ')}${chs.length>3?` and ${chs.length-3} more`:''}): a break is reported ${chs.length+1} times`,
        now:`Retired: ${chs[0].kept_name} checks exactly the same (Restore in History brings them back)`},
      ch=>`Duplicate of ${ch.kept_name}`.slice(0,300),reasonOf)]:[]),
    ...(past.length?[retireSection(past.map(c=>[c]),'Ended rules','Their dates are past and no rota covers them.',chs=>String(chs[0].name),chs=>chs[0],()=>ENDED_REASON,reasonOf)]:[])];
  // Nothing to propose (or no right to): the ended rules only.
  if(!canGroup||!groups.length&&!changes.length){
    if(!ended.length){notify('Nothing here for you: a coordinator proposes grouped rules and changes.','info');return;}
    showDialog('Group similar rules','Rules that have ended or copy another rule. Retiring one is recorded, and Restore in History brings it back.',body=>{
      body.append(...retireSections(()=>'Retired from Group similar rules'),...(groups.length||changes.length?[note(`${plural(groups.length+changes.length,'group or change','groups or changes')} can be proposed by a coordinator.`)]:[]));});
    return;}
  const range=(g:Row)=>!g.amounts?'':g.amounts[0]===g.amounts[1]?String(g.amounts[0]):`${g.amounts[0]}–${g.amounts[1]}`;
  formDialog('Group similar rules','Rules that say the same thing for different people, programmes, clinics or dates become one rule with lines. Nothing changes until an approver activates each new rule, which retires the rules it replaces. Retiring the new rule brings them back.',form=>{
    const picks=groups.map(g=>{
      const facts=[g.shape.split('+').map((s:string)=>shapeWords[s]||'').filter(Boolean).join(', '),g.people?plural(g.people,'person','people'):'',g.lines&&g.lines!==g.people?plural(g.lines,'line'):'',
        range(g)?`${VALUE_LABEL[g.kind]||'Number'} ${range(g)}`:'',`${formatDate(g.starts_on)} – ${g.ends_on?formatDate(lastIncludedDate(g.ends_on)):'no end'}`,
        g.calendar?`Academic calendar: ${g.calendar.name}${g.calendar.entry_id?'':' (added to the calendar)'}`:''].filter(Boolean).join(' · ');
      const c=check(g.shape==='protect'?`${g.name}: link to the academic calendar`:`${g.name}: ${plural(g.rule_ids.length,'rule')} into one`,!focus.length||g.rule_ids.some((id:string)=>focus.includes(id)),facts);
      if(g.switched_off)c.wrap.querySelector('.check-text')?.append(badge('Overtime switched off now: the limits it needs are no longer in force','warn','group-off'));
      c.wrap.classList.add('group-pick');c.input.addEventListener('change',count);
      // The new rule's name, editable in place (sent only when changed).
      const proposed=String(g.payload.name||g.name),rename=g.shape==='protect'?null:el('input',{type:'text',class:'group-name',value:proposed,maxLength:160,required:true,'aria-label':`Name of the new rule for ${g.name}`}) as HTMLInputElement|null;
      const row=el('div',{class:'group-row'},c.wrap,rename?el('label',{class:'group-name-field'},el('span',{class:'change-label'},'Name'),rename):'');
      return {g,c,row,rename,proposed};});
    const changePicks=changes.map(ch=>{
      const c=check(`${changeTitles[ch.shape]||'Change'}: ${ch.name}`,false);
      c.wrap.classList.add('group-pick');
      // A change may propose more than one rule (e.g. a Balance goal beside the order): say how many.
      const more=ch.extra?.length?el('p',{class:'muted change-count'},`${plural(ch.rule_ids.length,'rule')} → ${1+ch.extra.length}`):'';
      return {ch,c,row:el('div',{class:'group-row change-row'},c.wrap,changeSummary('',ch),more)};});
    const counter=el('p',{class:'group-count',role:'status','aria-live':'polite'});
    function count(){for(const p of picks)if(p.rename)p.rename.disabled=!p.c.input.checked;
      const chosen=picks.filter(p=>p.c.input.checked&&p.g.shape!=='protect');
      const rules=chosen.reduce((n,p)=>n+p.g.rule_ids.length,0);
      counter.replaceChildren(el('strong',{},`${plural(rules,'rule')} → ${plural(chosen.length,'rule')}`),el('span',{class:'muted'},` · ${preview.active} active rules become ${preview.active-rules+chosen.length}`));
      toggle.textContent=picks.every(p=>p.c.input.checked)?'Clear all':'Select all';}
    const toggle=el('button',{type:'button',class:'button ghost small'},'Clear all') as HTMLButtonElement;
    toggle.addEventListener('click',()=>{const on=!picks.every(p=>p.c.input.checked);for(const p of picks)p.c.input.checked=on;count();form.dispatchEvent(new Event('change',{bubbles:true}));});
    const find=panelSearch('Find a group',`Search ${plural(groups.length,'group')}`,text=>{const q=text.toLocaleLowerCase();for(const p of picks)p.row.hidden=!!q&&!String(p.g.name).toLocaleLowerCase().includes(q);},'group-search');
    find.hidden=groups.length<=8;
    const link=check('Link protected time to the academic calendar',true,'Protected academic days become entries on each programme’s academic calendar, so the rule follows the calendar.');
    const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=400;
    form.append(...(groups.length?[el('div',{class:'span-2 group-head'},counter,el('span',{class:'grow'}),find,toggle),el('div',{class:'span-2 group-list'},...picks.map(p=>p.row))]:[]),
      ...(groups.some(g=>g.calendar)?[link.wrap]:[]),
      ...(changes.length?[el('section',{class:'span-2 group-changes','aria-label':'Changes that are not identical'},el('h3',{},'Changes that are not identical'),
        el('p',{class:'hint'},'These change what auto-schedule does, so each is proposed only when you tick it. Like a group, nothing changes until an approver activates it, and retiring the new rule brings the old one back.'),
        el('div',{class:'group-list'},...changePicks.map(p=>p.row)))]:[]),
      ...retireSections(()=>reason.input.value.trim().length>=3?reason.input.value.trim():'Retired from Group similar rules'),
      ...(problems.length?[el('section',{class:'span-2 group-problems','aria-label':'Similar rules kept apart'},el('h3',{},`Kept apart · ${problems.length}`),
        el('ul',{},...problems.map(g=>el('li',{},el('strong',{},g.name),`: ${plural(g.rule_ids.length,'rule')} · ${why(g)}`))))]:[]),reason.wrap);
    count();
    return async()=>{
      const chosen=picks.filter(p=>p.c.input.checked),ticked=changePicks.filter(p=>p.c.input.checked);
      if(!chosen.length&&!ticked.length)throw new Error(changes.length?'Choose at least one group or change.':'Choose at least one group.');
      const names=Object.fromEntries(chosen.filter(p=>p.rename&&p.rename.value.trim()&&p.rename.value.trim()!==p.proposed).map(p=>[p.g.rule_ids[0],p.rename!.value.trim()]));
      await proposeChanges({groups:chosen.map(p=>p.g.rule_ids),changes:ticked.map(p=>p.ch.rule_ids),names,reason:reason.input.value,link_calendar:link.input.checked});
    };
  },'Propose grouped rules');
}
/** Which active rules apply to a person on a day, the number they get, and why (their own line, the rule's
 *  conditions, or another rule that replaces it for them). */
function whichRuleDialog(personId=''){
  showDialog('Which rule applies?','Every active department rule that applies to a person on a day, the number they get and why.',body=>body.append(whichRuleLookup(personId)));
}
/** A rule's reach on the planner: opens the planner with everything the rule applies to highlighted. */
function showOnPlanner(family:Row[],labelled=false){
  const live=family.find(p=>p.status==='active');
  if(!live||!permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR))return null;
  const go=()=>{closeRulePanel();highlightRule(live.id);navigate('schedule');};
  return labelled?compact(button('Show on schedule',go,'ghost small'),'calendar'):iconButton('Show on the schedule','calendar',go,{class:'bare rule-on-planner','data-tip':'Show on the schedule'});
}
/** The same dated exception list in legacy Rules and the catalogue's on-demand panel. */
function exceptionsTable(changed:()=>Promise<void>=refresh,personId?:string,linked?:string):HTMLElement{
  const excStage=(st:string):[string,Tone]=>[({proposed:statusLabel('proposed'),active:'Approved',approved:'Approved',retired:'Ended',ended:'Ended',withdrawn:'Withdrawn'} as Record<string,string>)[st]||st,statusTone(st)];
  const list=rows('exceptions').filter(r=>!personId||r.person_id===personId),rule=(r:Row)=>labelOf(referencePolicy(r.policy_id));
  const table=dataTable<Row>({id:personId?'person-exceptions':'exceptions',noun:['exception','exceptions'],rows:list,rowId:r=>r.id,
    // A person's few exceptions are a plain list in their hub; the Rules panel keeps the full collection.
    searchLabel:'Search exceptions',searchPlaceholder:'Rule, person or reason',resultScope:()=>personId||'',toolbar:personId?false:undefined,
    empty:['No exceptions yet','A person can get a dated exception to a rule that allows exceptions (Details › Exceptions: Allowed with approval).'],defaults:personId?{sort:['dates','desc'],wrap:['rule','reason']}:{group:'status',wrap:['rule','person','status','reason']},
    columns:[
      {key:'rule',label:'Rule',text:rule,facet:rule,cell:r=>{
        const p=referencePolicy(r.policy_id);if(!p)return rule(r);
        // Not the shared busy button: it would blur while a retired version loads, and Back returns focus here.
        const link=el('button',{type:'button',class:'link-button'},labelOf(p));
        link.addEventListener('click',()=>{void openRuleSource(r.policy_id).catch(e=>reportError(e));});return link;}},
      ...(!personId?[{key:'person',label:'Person',text:(r:Row)=>name('people',r.person_id),facet:(r:Row)=>name('people',r.person_id),cell:(r:Row)=>personSource(r.person_id,personName(r.person_id,name('people',r.person_id)))}]:[]),
      {key:'dates',label:'Dates',sort:r=>String(r.starts_on),cell:r=>dateUnits([formatDate(r.starts_on,zone()),'– '+formatDate(addDays(r.ends_on,-1),zone())])},
      {key:'status',label:'Status',facet:r=>excStage(r.status)[0],cell:r=>badge(...excStage(r.status))},
      {key:'reason',label:'Reason',text:r=>String(r.reason||''),cell:r=>el('span',{class:'swap-reason'},r.reason||'—')},
    ],
    // What this reader may do (the server's can_approve / can_end / can_undo); each decision is governed and audited.
    // The approver decides on the facts: the rule and what it asks, the person, the dates and the reason given, with
    // Which rule applies? for that person.
    // A linked record (Home › Rule exception) is marked and brought into view.
    decorate:(r,node)=>{if(r.id===linked){node.classList.add('dt-linked');node.setAttribute('aria-current','true');requestAnimationFrame(()=>node.scrollIntoView({block:'nearest'}));}},
    rowButtons:r=>r.can_approve===true?[button('Approve',()=>{const p=find('policies',r.policy_id),asks=p?.definition?.rule?ruleSentence(p.definition.rule,p.definition):'';
      confirmAction('Approve bounded exception',[name('policies',r.policy_id),asks,name('people',r.person_id),`${formatDate(r.starts_on,zone())} – ${formatDate(addDays(r.ends_on,-1),zone())}`,r.reason?`“${r.reason}”`:''].filter(Boolean).join(' · '),
        async reason=>{await command('exception.approve',{reason},r.id,r.revision);await changed();},'Confirm',
        canLookupRules()?el('p',{},button('Which rule applies?',()=>whichRuleDialog(r.person_id),'',{class:'link-button'})):undefined);},'small',{'aria-label':'Approve exception'}),
      // Declining is the server's end of a request (exception.end: kept in the history as withdrawn, audited; Undo restores it).
      ...(r.can_end===true?[button('Decline',()=>confirmAction('Decline exception request',`${name('people',r.person_id)} · ${name('policies',r.policy_id)}. The request is kept in the history; Undo restores it.`,
        async reason=>{await command('exception.end',{reason},r.id,r.revision);notify('Exception request declined.');await changed();},'Decline'),'secondary small',{'aria-label':'Decline exception'})]:[])]:[],
    rowActions:r=>{const who=`${name('people',r.person_id)} · ${name('policies',r.policy_id)}`,decide=(kind:string,title:string,text:string,label:string,said:string)=>
      confirmAction(title,text,async reason=>{await command(kind,{reason},r.id,r.revision);notify(said);await changed();},label);
      // A request the reader can approve is decided with Approve; withdrawing stays with its proposer (one decision per row).
      const withdrawn=r.status==='withdrawn',end=r.can_end===true&&!(r.status==='proposed'&&r.can_approve===true);
      return [...(end?[r.status==='proposed'&&r.created_by!==me!.identity_id?{label:'Decline request…',icon:'close' as const,run:()=>decide('exception.end','Decline exception request',`${who}. The request is kept in the history; Undo restores it.`,'Decline','Exception request declined.')}
          :r.status==='proposed'?{label:'Withdraw request…',icon:'close' as const,run:()=>decide('exception.end','Withdraw exception request',`${who}. The request is kept in the history; Undo restores it.`,'Withdraw','Exception request withdrawn.')}
          :{label:'End exception…',icon:'close' as const,danger:true,run:()=>decide('exception.end','End exception',`${who}. The rule applies to this person again in drafts; published rotas keep what they were approved under. Undo restores it.`,'End exception','Exception ended.')}]:[]),
        ...(r.can_undo===true?[{label:'Undo',icon:'history' as const,run:()=>decide('exception.undo',withdrawn?'Restore exception request':'Restore exception',`${who}. ${withdrawn?'It waits for approval again.':'Its original approval and dates apply again.'}`,'Undo',withdrawn?'Exception request restored.':'Exception restored.')}]:[])];},
  });
  // Retired rules are history outside the catalogue (21e096a0d): their names load once, then the table redraws.
  if(!retiredRules&&list.some(r=>!referencePolicy(r.policy_id)))void loadRetired().then(()=>{if(table.root.isConnected)table.refresh();}).catch(()=>{});
  return table.root;
}
/** A rule row from Simplify rules' blockers: a catalogue rule's line opens on its page; an older rule's person row opens in
 *  its change form (or the rule's detail for those who cannot change it). */
function openRuleRow(id:string,at:{lineId?:string;personId?:string}){
  const p=find('policies',id),rule=p?.definition?.rule;
  if(rule?.kind==='catalogue'&&at.lineId){navigate(`rules/${rule.id}/${at.lineId}`);return;}
  const i=at.personId?((p?.definition?.per_person||[]) as Row[]).findIndex(r=>r.person_id===at.personId):-1;
  if(p&&i>=0&&p.status!=='retired'&&may('rule.propose'))ruleChangeForm(ctx(),p,i);
  else void openRuleSource(id,at.personId).catch(e=>reportError(e));
}
function exceptionsPanel(linked?:string){
  dockPanel({key:'rule-exceptions',title:'Exceptions',width:760,build:({body,close})=>{
    const changed=async()=>{close();await refresh();};
    const request=button('Request exception',()=>exceptionForm({...referenceContext(),refresh:changed}),'secondary');request.hidden=true;
    if(permits(CAPABILITY.COORDINATOR)){body.append(request);void loadRetired().then(()=>{request.hidden=!referenceContext().catalogues.policies.some(r=>r.status==='active'&&allowsException(r));}).catch(e=>reportError(e));}
    body.append(exceptionsTable(changed,undefined,linked));
  }});
}
async function rulesPage(target:HTMLElement){
  retiredRules=null;retiredVisit++;draftChoice=null;draftMaps=new Map();
  void get<{reach:Record<string,number>}>('/api/policies/reach').then(r=>{reachNow=r.reach;
    for(const n of target.querySelectorAll<HTMLElement>('.rl-count[data-reach]')){const v=reachNow[n.dataset.reach!];n.textContent=v==null?'':` · ${plural(v,'person','people')}`;}}).catch(()=>{});
  const canPropose=may('rule.propose'),policies=rows('policies'),all=policies.filter(p=>p.definition);
  // Retired versions are history outside the catalogue (21e096a0d): with no current rule, one row says whether any exist.
  const none=!policies.length&&!await get<{policies:Row[]}>('/api/policies?status=retired&limit=1').then(r=>r.policies.length,()=>0);
  const people=new Map(rows('people').map(p=>[p.id,String(p.display_name||'')]));
  const ruleStem=(p:Row)=>ruleStemFor(p,people);
  // A grouped rule, or one with lines, is one rule. Older separate copies of one rule (imported per person, per week or
  // per weekday) still collapse into one family with a table of its copies.
  const familyKey=(p:Row)=>p.definition.listed_only||p.definition.merged_ids?.length||p.definition.per_person?.length?'id:'+p.id:ruleFamilyFor(p,people);
  const families=(list:Row[])=>{const m=new Map<string,Row[]>();for(const p of list){const k=familyKey(p),group=m.get(k);if(group)group.push(p);else m.set(k,[p]);}return [...m.values()];};
  const inForce=limitsInForce(),problems=new Map(all.map(p=>[p.id,problemsOf(p,inForce)]));
  const needs=(p:Row)=>p.status==='proposed'||!!problems.get(p.id)?.length;
  const current=families(all.filter(p=>p.status!=='retired')),attention=current.filter(f=>f.some(needs));
  const switchedOff=all.filter(p=>problems.get(p.id)?.length);
  const pendingExceptions=rows('exceptions').filter(r=>r.status==='proposed').length;
  // Retired versions are history: never counted here. One primary action: Add rule.
  target.append(sectionHead('Rules',current.length?`${plural(current.length,'rule')}${attention.length?` · ${attention.length} ${attention.length===1?'needs':'need'} attention`:''}`:'Rules apply once an approver activates them.',
    canPropose?button('Add rule',()=>policyForm(ctx()),'primary'):null,
    (canPropose||permits(CAPABILITY.APPROVER))&&all.length?compact(button('Group similar rules',()=>groupRulesDialog(),'secondary'),'layers'):null,
    canLookupRules()&&all.length?compact(button('Which rule applies?',()=>whichRuleDialog(),'secondary'),'search'):null,
    // Exceptions are one counted control (as in the catalogue), not a section under the list: the panel holds the
    // collection, its approvals and Request exception.
    compact(button(pendingExceptions?`Exceptions (${pendingExceptions} pending)`:'Exceptions',()=>exceptionsPanel(),'secondary'),'shield')));
  if(none&&!canPropose)target.append(empty('No rules yet','Coordinators add rules; they appear here for you to approve before they apply.',null,'rules'));
  else if(none){
    // First rules: the common ones one click away (each still goes through approval).
    const starters=['min_rest','max_hours','max_consecutive_days','minimum_cover','max_consecutive_weekends','block_quota','balance_goal','run_length','days_off_run'];
    target.append(el('section',{class:'panel rule-starters'},el('h3',{},'Start with a common rule'),el('p',{class:'muted'},'Basic staffing and overlap checks always apply. Add the rules your department follows; an approver activates each one.'),
      el('div',{class:'rule-card-grid'},...RULE_KINDS.filter(k=>starters.includes(k[0])).map(([kind,title,desc,eg])=>{const b=el('button',{type:'button',class:'rule-card starter'},el('span',{class:'rule-card-text'},el('strong',{},title),el('span',{},desc),el('small',{},'e.g. '+eg)));
        b.addEventListener('click',()=>policyForm(ctx(),'',{kind}));return b;})),
      button('See every rule type',()=>policyForm(ctx()),'ghost')));
  }
  const search={input:{value:rulesView.query}};  // the shared toolbar's search box writes here
  // The status strip is the Saved views' built-in views, each with its count.
  const TABS:[string,string][]=[['','Current'],['attention','Needs attention'],['proposed',statusLabel('proposed')],['retired','History']];
  const state={input:{value:TABS.some(([v])=>v===rulesView.status)?rulesView.status:''}};
  const statusPanelId='rule-status-panel';
  const tabCount=(value:string)=>value==='retired'?(retiredRules?families(retiredRules).length:null):value==='attention'?attention.length:value?families(all.filter(p=>p.status===value)).length:current.length;
  const effective=field('Effective on','date',rulesView.date);
  const list=el('div',{id:statusPanelId,role:'region','aria-label':'Current'}),summary=el('p',{class:'rule-summary',role:'status','aria-live':'polite'});
  // Topic, severity and who/where filters (the same picker and condition builder as every list and rule form).
  const topicNames=[...RULE_TOPICS.map(t=>t.name),'Other rules'];
  const filterCount=()=>rulesView.topics.length+rulesView.severity.length+(rulesView.who?1:0);
  // Named views on the server (table_view 'rules'): the status view, filters, grouping and sort; never the search or date.
  type RulesSaved={filters:Record<string,string[]>;conditions:Conditions|null;group:string;sort:SortValue};
  const definition=():RulesSaved=>({filters:{...(state.input.value?{status:[state.input.value]}:{}),...(rulesView.topics.length?{topic:rulesView.topics}:{}),...(rulesView.severity.length?{severity:rulesView.severity}:{})},
    conditions:rulesView.who,group:rulesView.group,sort:rulesView.sort});
  const same=(d:RulesSaved)=>{const n=definition();return JSON.stringify([d.filters||{},d.conditions||null,d.group??'',d.sort||null])===JSON.stringify([n.filters,n.conditions||null,n.group,n.sort||null]);};
  const dirty=(d:RulesSaved)=>!same(d);
  const applyView=(d:RulesSaved|null)=>{const f=d?.filters||{};state.input.value=TABS.some(([v])=>v===f.status?.[0])?f.status![0]:'';
    rulesView={...rulesView,topics:f.topic||[],severity:f.severity||[],who:d?.conditions||null,group:d?d.group??'':'topic',sort:d?.sort||null};};
  let savedRules:SavedView<RulesSaved>[]=[],rulesViewId=rulesSavedId;
  const openView=(id:string)=>{rulesViewId=rulesSavedId=id;};
  void fetchViews<RulesSaved>('rules').then(v=>{savedRules=v;const open=openingView('rules',v);
    if(open!==undefined&&(open?.id||'')!==rulesViewId){openView(open?.id||'');applyView(open?open.definition:null);renderRules();return;}
    if(rulesViewId&&!v.some(x=>x.id===rulesViewId))openView('');if(alive(toolbar.root))toolbar.sync();}).catch(()=>undefined);
  const needWords=()=>`${attention.length} ${attention.length===1?'needs':'need'} attention`;
  const builtins=()=>TABS.map(([value,label])=>{const n=tabCount(value);return {label,count:n===null?undefined:n,on:state.input.value===value,
    warn:value==='attention'&&attention.length?needWords():undefined,apply:()=>{openView('');state.input.value=value;renderRules();}};});
  // The shared collection toolbar (table_view.ts), in its one order: Saved views (the status views first) → Effective on
  // (page scope) → Search → Filter → Group → Sort. Rules have one layout, so there are no View settings.
  const toolbar=collectionToolbar({label:'Rules',scope:[effective.wrap],
    saved:{name:()=>savedRules.find(x=>x.id===rulesViewId)?.name||'',defaultName:()=>TABS.find(([v])=>v===state.input.value)![1],
      dirty:()=>{const a=savedRules.find(x=>x.id===rulesViewId);return !!a&&dirty(a.definition);},
      warn:()=>attention.length&&state.input.value!=='attention'?needWords():'',
      items:close=>savedViewItems<RulesSaved>({key:'rules',noun:'rules',saved:savedRules,activeId:rulesViewId,current:definition,dirty,builtins:builtins(),close,
        apply:v=>{openView(v?.id||'');applyView(v?v.definition:null);renderRules();},
        done:(next,id)=>{savedRules=next;openView(id);toolbar.sync();},
        reset:()=>{applyView({filters:state.input.value?{status:[state.input.value]}:{},conditions:null,group:'topic',sort:null});renderRules();}})},
    search:{value:rulesView.query,label:'Find rules',placeholder:'Rule, person, programme or clinic',onInput:q=>{search.input.value=q;},apply:()=>renderRules(),
      options:{value:()=>rulesView.search_options,set:o=>{rulesView={...rulesView,search_options:o};},can:serverCapability('policies')!}},
    filter:{count:filterCount,clear:()=>{rulesView={...rulesView,topics:[],severity:[],who:null};renderRules();},
      // Active filters live inside Filter (its label and popover), never in a chip row.
      active:()=>{const whoText=conditionsText(rulesView.who,namer(ctx()).label);
        return [...rulesView.topics.map(t=>({name:'Topic',value:t,remove:()=>{rulesView.topics=rulesView.topics.filter(x=>x!==t);renderRules();}})),
          ...rulesView.severity.map(v=>({name:'Severity',value:v[0].toUpperCase()+v.slice(1),remove:()=>{rulesView.severity=rulesView.severity.filter(x=>x!==v);renderRules();}})),
          ...(whoText?[{name:'Names',value:whoText,remove:()=>{rulesView.who=null;renderRules();}}]:[])];},
      build:(panel,changed)=>{panel.classList.add('rule-filter-pop');const refilter=()=>{renderRules();changed();};
    // Names only: a date set (and its entries) is when a rule applies, not a name it mentions.
    const who=conditionBuilder(ruleConditionFields(ctx()).filter(f=>f.key!=='date_set_ids'&&f.key!=='date'),rulesView.who);
    who.root.addEventListener('change',()=>{rulesView.who=who.read();refilter();});
    panel.append(el('div',{class:'rule-filter-row'},
      el('div',{},el('div',{class:'dt-pop-label'},'Topic'),multiPick('Topic',topicNames.map(t=>({value:t,label:t})),rulesView.topics,v=>{rulesView.topics=v;refilter();},'Any topic').wrap),
      el('div',{},el('div',{class:'dt-pop-label'},'Severity'),multiPick('Severity',[{value:'mandatory',label:'Mandatory'},{value:'advisory',label:'Advisory'}],rulesView.severity,v=>{rulesView.severity=v;refilter();},'Any').wrap)),
      el('div',{class:'dt-pop-label'},'Names'),el('p',{class:'hint'},'Rules that name these people, ranks, programmes, clinics or duty types in their conditions or lines.'),who.root);
  }},
    group:{options:[{key:'topic',label:'Topic'},{key:'severity',label:'Mandatory or advisory'}],value:()=>rulesView.group,set:k=>{rulesView.group=k;renderRules();},active:()=>rulesView.group!=='topic',defaultKey:'topic'},
    sort:{options:[{key:'name',label:'Name'},{key:'people',label:'People reached'},{key:'start',label:'Start date'}],value:()=>rulesView.sort,set:v=>{rulesView.sort=v;renderRules();},active:()=>!!rulesView.sort}});
  // Older copies of the same family this proposal would duplicate (the approver chooses to retire them). The rule a
  // new version replaces, and the rules grouped into it, are retired by the server anyway: they are not choices.
  const replacesFor=(p:Row)=>p.status!=='proposed'?[]:all.filter(o=>o.status==='active'&&o.id!==p.id&&o.id!==p.definition.replaces_id&&!(p.definition.merged_ids||[]).includes(o.id)
    &&familyKey(o)===familyKey(p)&&targetKey(o.definition.scope)===targetKey(p.definition.scope)&&o.starts_on<(p.ends_on||'9999-12-31')&&(!o.ends_on||o.ends_on>p.starts_on));
  const retireOrDecline=(picked:Row[]):BulkAction[]=>{
    const proposed=picked.filter(p=>p.status==='proposed'),active=picked.filter(p=>p.status==='active'),off=picked.filter(p=>problems.get(p.id)?.length);
    const activatable=proposed.filter(p=>mayDecide(me!,'rule.activate',p.created_by===me!.identity_id));
    return [...(activatable.length&&may('policy.activate')?[{label:`Activate ${activatable.length}`,icon:'check' as const,run:(_:string[],d:()=>void)=>{d();void ruleReview(activatable.length===1?activatable[0]:activatable,permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),true,refresh,name,zone(),false,activatable.flatMap(replacesFor),ruleStem(activatable[0]),'',ctx());}}]:[]),
      ...(off.length&&canPropose?[{label:`Switch ${off.length} overtime back on`,icon:'check' as const,run:(_:string[],d:()=>void)=>{d();void fixOvertime(off);}}]:[]),
      ...(proposed.length?[{label:`Decline ${proposed.length} proposed`,icon:'close' as const,danger:true,run:(_:string[],d:()=>void)=>confirmAction(`Decline ${proposed.length} proposed rules`,'They never apply. Each stays in History.',async reason=>{await runBulk({collection:'policies',noun:['rule','rules'],destructive:true,verb:'Decline',message:'{n} rules declined.',onDone:async()=>{d();await refresh();},
        body:{item_kind:'policy.decline',items:proposed.map(p=>({target_id:p.id,expected_revision:p.revision})),reason}});},'Decline rules')}]:[]),
      ...(active.length&&may('policy.retire')?[{label:`Retire ${active.length} active`,icon:'lock' as const,danger:true,run:(_:string[],d:()=>void)=>confirmAction(`Retire ${active.length} active rules`,`${active.some(safetyRetirement)?SAFETY_RETIREMENT_NOTICE+' ':''}They stop applying to rotas being planned. Published rotas keep the rules they were approved under.`,async reason=>{await runBulk({collection:'policies',noun:['rule','rules'],destructive:true,verb:'Retire',message:'{n} rules retired.',onDone:async()=>{d();await refresh();},
        body:{item_kind:'policy.retire',items:active.map(p=>({target_id:p.id,expected_revision:p.revision})),reason}});},'Retire rules')}]:[])];
  };
  // Select rules (every line of each) to activate, decline or retire together.
  const pool=()=>[...all,...(retiredRules||[])];
  const ruleBulk=may('policy.decline')?bulkSelection(['rule','rules'],ids=>retireOrDecline(pool().filter(p=>ids.includes(p.id)))):null;
  const textOf=(policy:Row)=>{
    const scope=policy.definition.scope;
    const labels=[['person_id','people'],['category_id','categories'],['programme_id','programmes'],['rotation_type_id','rotation_types'],['assignment_type_id','assignment_types'],['service_id','services'],['location_id','locations']].map(([key,kind])=>scope[key]?name(kind,scope[key]):'');
    labels.push(...(scope.person_ids||[]).map((id:string)=>name('people',id)),...(scope.category_ids||[]).map((id:string)=>name('categories',id)),...(scope.service_ids||[]).map((id:string)=>name('services',id)),...(scope.capability_ids||[]).map((id:string)=>name('capabilities',id)),...(scope.tags||[]),conditionWords(scope));
    for(const l of policy.definition.per_person||[])labels.push(l.person_id?name('people',l.person_id):'',l.programme_id?name('programmes',l.programme_id):'',...(l.service_ids||[]).map((id:string)=>name('services',id)));
    return [policy.name,policy.owner,policy.source,policy.definition.rule.kind.replaceAll('_',' '),ruleTopic(policy.definition.rule).name,...labels].join(' ').toLocaleLowerCase();
  };
  const texts=new Map<string,string>();const text=(p:Row)=>{let t=texts.get(p.id);if(t===undefined){t=textOf(p);texts.set(p.id,t);}return t;};
  // With search options: a rule's own words, and apart each record its scope names (by name, and by id for a picked one;
  // a tag by id or name). Never who the rule binds.
  const tagIds=new Map(rows('tags').map(t=>[String(t.name).toLowerCase(),t.id as string]));
  const operands=(t:Conditions|null|undefined):{field:string;value:string}[]=>(t?.conditions||[]).flatMap(c=>'conditions' in c?operands(c):(c.values||[]).map(v=>({field:c.field,value:String(v)})));
  const SCOPE_KEYS:[string,string][]=[['person_id','people'],['category_id','categories'],['programme_id','programmes'],['rotation_type_id','rotation_types'],['assignment_type_id','assignment_types'],['service_id','services'],['location_id','locations'],
    ['person_ids','people'],['category_ids','categories'],['service_ids','services'],['capability_ids','capabilities']];
  const policyRefs=(p:Row):[string,string][]=>{const scope=p.definition.scope||{};
    return [...SCOPE_KEYS.flatMap(([key,kind])=>[scope[key]].flat().filter(Boolean).map((id:string):[string,string]=>[kind,id])),...(scope.tags||[]).map((t:string):[string,string]=>['tags',t]),
      ...operands(scope.conditions).map(x=>[RULE_FIELDS.find(f=>f.key===x.field)?.kind||(x.field==='tags'?'tags':''),x.value] as [string,string]),
      ...(p.definition.per_person||[]).flatMap((l:Row)=>[...(l.person_id?[['people',l.person_id]]:[]),...(l.programme_id?[['programmes',l.programme_id]]:[]),...(l.service_ids||[]).map((id:string)=>['services',id])] as [string,string][])];};
  const policyRecord=(p:Row)=>({own:()=>[p.name,p.owner,p.source,p.definition.rule.kind.replaceAll('_',' '),ruleTopic(p.definition.rule).name],id:p.id,
    related:()=>policyRefs(p).map(([kind,id])=>kind==='tags'?id.replace(/^#/,''):kind?name(kind,id):id),
    refs:()=>policyRefs(p).flatMap(([kind,id])=>kind==='tags'?[id,tagIds.get(id.replace(/^#/,'').toLowerCase())||'']:[id])});
  const SECTION_PAGE=50;
  const legendKey=legend('Key');legendKey.root.classList.add('rl-legend');
  const MARKS_TEXT:Record<string,string>={attn:'Needs attention',prop:'Waiting for approval',mand:'Mandatory',adv:'Advisory'};
  const markEl=(kind:string,tip=MARKS_TEXT[kind])=>el('span',{class:'rl-mark '+kind,title:tip},el('span',{class:'sr-only'},MARKS_TEXT[kind]),
    kind==='mand'?icon('shield'):kind==='prop'?icon('clock'):kind==='attn'?icon('alert'):el('span',{'aria-hidden':'true'}));
  /** Where a rule is in the list as filtered and sorted (open groups, including rules behind "Show N more"): ↑ ↓ in its
   *  side panel open the rule above or below, drawing more of its group first when needed. */
  let ruleOrder:{ids:string;group:string}[]=[];
  const rulePosition=(family:Row[]|string):PanelPosition|undefined=>{
    const ids=typeof family==='string'?family:family.map(p=>p.id).join(' '),order=ruleOrder,i=order.findIndex(x=>x.ids===ids);
    return i<0?undefined:{index:i,total:order.length,noun:'rule',go:j=>{const x=ruleOrder[j];if(!x)return;
      if(!list.querySelector(ruleMark(x.ids))){rulesView.more=[...rulesView.more,x.group];renderRules();}
      list.querySelector(ruleMark(x.ids))?.querySelector<HTMLElement>('.rl-text')?.click();}};};
  /** One rule (or an older family of copies) as a row: one mark, what it asks in a sentence, where and its name, its
   *  numbers, who it reaches; the row opens its sheet. */
  const ruleRow=(family:Row[],sizes:Map<string,number>)=>{
    const policy=family[0],single=family.length===1,rule=policy.definition.rule;
    const lineOf=single&&(sizes.get(familyKey(policy))||0)>1;
    const ruleName=single&&!lineOf?labelOf(policy):ruleStem(policy);
    // A rule whose lines use different numbers reads as a range ("Aim for 6–8 session units each week").
    const key=VALUE_KEY[rule.kind],values=key?family.flatMap(p=>numbersOf(p,key)):[];
    const lo=Math.min(...values),hi=Math.max(...values);
    const shapes=new Map<string,number>();for(const p of family){const k=JSON.stringify({...p.definition.rule,...(key?{[key]:0}:{}),starts_on:0,ends_on:0});shapes.set(k,(shapes.get(k)||0)+1);}
    const common=family.find(p=>JSON.stringify({...p.definition.rule,...(key?{[key]:0}:{}),starts_on:0,ends_on:0})===[...shapes].sort((a,b)=>b[1]-a[1])[0][0])!.definition.rule;
    const scope=policy.definition.scope||{},sameScope=family.every(p=>JSON.stringify(p.definition.scope||{})===JSON.stringify(scope));
    const plain=ruleSentence(values.length?{...common,[key!]:lo===hi?lo:`${lo}–${hi}`}:common,single?policy.definition:sameScope?{scope}:undefined)+(shapes.size>1?' Some lines differ.':'');
    // A catalogue rule is titled by its catalogue entry; what the entry is for leads the line below.
    const entry=catalogueEntryOf(rule),title=entry?.name||plain.replace(/\.$/,'');
    const trouble=family.flatMap(p=>problems.get(p.id)||[]);
    const waiting=family.filter(p=>p.status==='proposed'),live=family.filter(p=>p.status!=='retired');
    const mark=trouble.length?'attn':waiting.length?'prop':(policy.effective_severity??policy.definition.severity)==='mandatory'?'mand':'adv';
    const keyMark=markEl(mark);keyMark.querySelector('.sr-only')!.remove();keyMark.removeAttribute('title');keyMark.setAttribute('aria-hidden','true');
    const merged=(policy.definition.merged_ids||[]).length,previousVersion=policy.definition.replaces_id?pool().find(p=>p.id===policy.definition.replaces_id):undefined;
    // Retired and declined rules say so (History); a rule simply in force or waiting says nothing more (its mark does).
    const states=[...new Set(family.map(ruleState))].filter(s=>s==='Retired'||s==='Declined');
    const sub=[entry?plain.replace(/\.$/,''):'',sameScope?whereShort(scope,rule,name):'Scope varies',...(single?[lineOf?ruleDates(policy):'',policy.status==='proposed'&&merged?`Replaces ${plural(merged,'rule')}`:'',
        policy.status==='proposed'&&previousVersion?`New version of ${labelOf(previousVersion)}`:'']
      :[plural(family.length,'copy','copies'),`${formatDate(policy.starts_on)} – ${family.some(p=>!p.ends_on)?'no end':formatDate(lastIncludedDate(family.reduce((m,p)=>p.ends_on>m?p.ends_on:m,'')))}`]),
      policy.definition.workspace_id?'One rota only':'',policy.definition.calendar_entry_id?'Protected time':'',ruleName===title?'':ruleName].filter(Boolean).join(' · ');
    const review=waiting.length&&may('policy.activate')?()=>ruleReview(single?policy:waiting,permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),mayDecide(me!,'rule.activate',waiting.some(p=>p.created_by===me!.identity_id)),refresh,name,zone(),false,waiting.flatMap(replacesFor),ruleName,plain,ctx()):undefined;
    const open=()=>single?ruleSheet(policy,title,inForce,review,rulePosition(family)):familySheet(family,title,ruleName,rulePosition(family));
    const text=el('button',{type:'button',class:'rl-text'},el('strong',{},title),el('span',{},...states.map(s=>badge(single?s:`${family.filter(p=>ruleState(p)===s).length} ${s.toLowerCase()}`,'neutral','rl-state')),sub));
    text.addEventListener('click',open);
    // The pill spans the rule and its lines (copies): "1–2" when they differ.
    const pill=limitPill(family.flatMap(p=>allLimits(p.definition)));
    const counted=live[0]?.id,people=counted!==undefined&&counted in reachNow?reachNow[counted]:null;
    // Only the names truncate (all of them in the tooltip and for screen readers); the count always shows.
    const whoAll=whoShort(scope,name,Infinity);
    const who=el('span',{class:'rl-who'},el('span',{class:'rl-who-text',title:whoAll},el('span',{'aria-hidden':'true'},whoShort(scope,name)),el('span',{class:'sr-only'},whoAll)),
      ...(counted?[el('span',{class:'rl-count','data-reach':counted},people==null?'':` · ${plural(people,'person','people')}`)]:[]));
    const pick=ruleBulk?(()=>{const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':'Select '+ruleName,checked:family.every(v=>ruleBulk.selected.has(v.id))}) as HTMLInputElement;
      b.addEventListener('change',()=>{for(const v of family)b.checked?ruleBulk.selected.add(v.id):ruleBulk.selected.delete(v.id);ruleBulk.show([]);});return b;})():el('span');
    const primary=review?button(single?'Review':`Review ${waiting.length}`,review,'primary small')
      :trouble.length&&canPropose?button('Switch back on…',()=>fixOvertime(family.filter(p=>problems.get(p.id)?.length)),'secondary small'):null;
    const toCheck=single?[policy]:live.slice(0,100);
    const more:HTMLButtonElement=opens(iconButton('More for '+ruleName,'dots',()=>menu(more,'More for '+ruleName,[
      {label:'Open details',icon:'rules',run:open},
      ...(live.length&&permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)?[{label:TERMS.checkRuleAgainstRotas,icon:'shield' as const,run:()=>void ruleReview(single?policy:toCheck,permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),permits(CAPABILITY.APPROVER)&&mayDecide(me!,'rule.activate',toCheck.some(p=>p.status==='proposed'&&p.created_by===me!.identity_id)),refresh,name,zone(),false,[],ruleName,plain,ctx())}]:[]),
      ...(single&&policy.status!=='retired'&&canPropose?[{label:'Change…',icon:'edit' as const,run:()=>ruleChangeForm(ctx(),policy)}]:[])]),{class:'bare','data-tip':'More'}),'menu');
    // The row's mark and numbers pill are drawn as their key entries (dom.ts legend; every mark has one).
    return el('section',{class:'rule-item rl-row','data-rule-ids':family.map(p=>p.id).join(' '),tabindex:'-1'},pick,
      legendKey.mark(markEl(mark,trouble[0]||MARKS_TEXT[mark]),'mark:'+mark,keyMark,MARKS_TEXT[mark],['attn','prop','mand','adv'].indexOf(mark)),text,
      pill?legendKey.mark(Object.assign(numbersEl([{text:pill}]),{title:pill}),'pill',numbersEl([{text:'≥ 2'}]),'Minimum · ≤ maximum · → target',4):el('span'),who,el('div',{class:'rl-actions'},primary,showOnPlanner(family),more));
  };
  /** An older family of copies (the same rule for different people or dates): its copies in a table. */
  const familySheet=(family:Row[],title:string,ruleName:string,position?:PanelPosition)=>{
    const policy=family[0],key=VALUE_KEY[policy.definition.rule.kind];
    rulePanel(family,title,ruleName,position,({body})=>{
      const content=el('div',{class:'rule-detail-content'},ruleFacts([['Classification',levelBadge(policy.effective_severity??policy.definition.severity,policy.severity_reason)],['Exception requests',allowsException(policy)?'Allowed with approval':'Not allowed']]));
      const groupable=LINE_KINDS.includes(policy.definition.rule.kind)&&family.filter(p=>p.status==='active').length>1;
      content.append(ruleLinesTable(family,key,retireOrDecline),
        ...(canPropose&&groupable?[el('div',{class:'actions'},el('span',{class:'muted'},'These copies say the same thing for different people or dates.'),
          button('Make one rule with lines',()=>groupRulesDialog(family.map(p=>p.id)),'secondary small'))]:[]));
      body.append(rulePanels(familyKey(policy),content,()=>el('div',{},...family.map(p=>el('section',{},el('h4',{},labelOf(p)),historySection(p)))),
        ()=>el('div',{},...family.map(p=>el('section',{},el('h4',{},labelOf(p)),ruleFacts([['Policy owner',p.owner||'Not recorded'],['Source or decision',p.source||'Not recorded'],['Effective',ruleDatesNode(p)]]))))));
    });
  };
  const renderRules=()=>{
    const query=search.input.value.trim().toLocaleLowerCase(),day=effective.input.value,status=state.input.value;
    legendKey.clear();
    list.setAttribute('aria-label',TABS.find(([v])=>v===status)![1]);  // the region is named after the status view shown
    rulesView={...rulesView,query:search.input.value,status,date:day};
    toolbar.sync();
    if(banner)banner.hidden=status==='attention';
    if(status==='retired'&&!retiredRules){
      list.replaceChildren(skeleton('table','Loading history…'));summary.textContent='';
      void loadRetired().then(()=>{if(alive(list)){toolbar.sync();if(state.input.value==='retired')renderRules();}}).catch(e=>{
        if(alive(list)&&state.input.value==='retired'&&!sessionChanged(e))list.replaceChildren(note((e as Error).message,'error',e),button('Retry loading history',renderRules,'secondary small'));
      });return;}
    const optioned=rulesView.search_options&&recordMatcher(search.input.value,rulesView.search_options);
    const lines=pool().filter(p=>(status==='attention'?needs(p):status?p.status===status:p.status!=='retired')&&(!day||p.starts_on<=day&&(!p.ends_on||day<p.ends_on))&&(rulesView.search_options?!optioned||optioned(policyRecord(p)):!query||text(p).includes(query))
      &&(!rulesView.topics.length||rulesView.topics.includes(ruleTopic(p.definition.rule).name))&&(!rulesView.severity.length||rulesView.severity.includes(p.effective_severity??p.definition.severity))&&namesMatch(p,rulesView.who));
    // Rules that have several lines anywhere (e.g. one waiting, the rest active): a lone line still reads as a line of that rule.
    const sizes=new Map<string,number>();for(const p of pool())if(status==='retired'?p.status==='retired':p.status!=='retired'){const k=familyKey(p);sizes.set(k,(sizes.get(k)||0)+1);}
    const entries=families(lines).map(f=>f.sort((a,b)=>String(a.starts_on).localeCompare(String(b.starts_on))));
    list.replaceChildren();toolbar.shown(entries.length,undefined,['rule','rules']);
    const filtered=!!(query||rulesView.search_options?.reference||day||filterCount());
    summary.textContent=!entries.length?'':status===''&&!filtered?`${plural(entries.length,'rule')}${attention.length?` · ${attention.length} ${attention.length===1?'needs':'need'} attention`:''}`
      :status==='retired'?plural(entries.length,'rule in history','rules in history'):plural(entries.length,'matching rule');
    if(!entries.length){list.append(status==='attention'&&!filtered?empty('Nothing needs attention','No rules are waiting for approval or have unresolved overtime settings.',null,'check')
      :status==='proposed'&&!filtered?empty('Nothing waiting for approval','Proposed rules and changes wait here until a different person approves them.',null,'check')
      :status==='retired'&&!filtered?empty('No history yet','Retired and declined rules appear here.',null,'history')
      :status===''&&!filtered?empty('No current rules','Retired and declined rules remain in History.',null,'rules')
      :empty('No matching rules','Change the search, the filters or the effective date.',null,'search'));return;}
    // Groups (topic by default: one definition, RULE_TOPICS; or mandatory/advisory; or none), each collapsible with its
    // count and what waits in it; big groups show 50 at a time. Within a group: by name, people reached or start date.
    const groupOf=(f:Row[]):string=>rulesView.group==='severity'?((f[0].effective_severity??f[0].definition.severity)==='mandatory'?'Mandatory':'Advisory'):rulesView.group==='topic'?ruleTopic(f[0].definition.rule).name:'';
    const groupNames=rulesView.group==='severity'?['Mandatory','Advisory']:rulesView.group==='topic'?topicNames:[''];
    const byGroup=new Map<string,Row[][]>(groupNames.map(t=>[t,[]]));
    for(const f of entries)byGroup.get(groupOf(f))!.push(f);
    const [sortKey,direction]=rulesView.sort||['name','asc'];ruleOrder=[];
    const sortValue=(f:Row[]):string|number=>sortKey==='people'?(reachNow[f[0].id]??-1):sortKey==='start'?String(f[0].starts_on):ruleStem(f[0]);
    for(const [groupName,fams] of byGroup){
      if(!fams.length)continue;
      fams.sort((a,b)=>{const x=sortValue(a),y=sortValue(b),c=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y));return (direction==='desc'?-c:c)||ruleStem(a[0]).localeCompare(ruleStem(b[0]));});
      if(!groupName||!rulesView.collapsed.includes(groupName))ruleOrder.push(...fams.map(f=>({ids:f.map(p=>p.id).join(' '),group:groupName})));
      const fill=(body:HTMLElement)=>{const shown=rulesView.more.includes(groupName)?fams:fams.slice(0,SECTION_PAGE);
        body.replaceChildren(...shown.map(f=>ruleRow(f,sizes)),...(shown.length<fams.length?[button(`Show ${fams.length-shown.length} more`,()=>{rulesView.more=[...rulesView.more,groupName];fill(body);},'ghost small')]:[]));};
      if(!groupName){const body=el('div',{class:'rule-group-body'});fill(body);list.append(el('section',{class:'rule-group','aria-label':'Rules'},body));continue;}
      const topic=RULE_TOPICS.find(t=>t.name===groupName),open=!rulesView.collapsed.includes(groupName);
      const waiting=fams.filter(f=>f.some(p=>p.status==='proposed')).length,off=fams.filter(f=>f.some(p=>problems.get(p.id)?.length)).length;
      const meta=[off?`${off} ${off===1?'needs':'need'} attention`:'',waiting?`${waiting} waiting for approval`:''].filter(Boolean).join(' · ');
      const id='rule-topic-'+groupNames.indexOf(groupName),body=el('div',{id,class:'rule-group-body',hidden:!open});
      const square=el('span',{class:'rule-group-icon duty-'+(topic?.hue||(groupName==='Mandatory'?'indigo':groupName==='Advisory'?'teal':'slate')),'aria-hidden':'true'},
        icon(topic?.icon||(groupName==='Mandatory'?'shield':groupName==='Advisory'?'sparkle':'rules')));
      // The square's hue and icon mean the group named beside it: the head's own words are its key.
      const head=groupToggle(el('span',{class:'rule-group-name'},groupName),fams.length,open,now=>{body.hidden=!now;if(now&&!body.childElementCount)fill(body);
        rulesView.collapsed=now?rulesView.collapsed.filter(t=>t!==groupName):[...rulesView.collapsed,groupName];},
        {cls:'rule-group-toggle',lead:square,extra:meta&&status!=='retired'?[el('span',{class:'rl-group-meta'},meta)]:[],attrs:{'aria-controls':id,'data-lg-self':''}});
      if(open)fill(body);
      list.append(el('section',{class:'rule-group','aria-label':groupName},el('h2',{class:'rule-group-head'},head),body));
    }
    markOpen(list,false,m=>{const x=ruleOrder.find(r=>ruleMark(r.ids)===m);return x&&rulePosition(x.ids);});
  };
  effective.input.addEventListener('change',()=>renderRules());effective.wrap.classList.add('rule-date','labelled');
  /** A rule just proposed is brought into view: when the tab, a filter, a closed topic or the 50-row page hides it,
   *  they are cleared for it (once) and the page drawn again; then it is scrolled to, highlighted and focused. */
  function revealProposed(){
    const ids=revealRules;if(!ids.length)return;
    const row=list.querySelector<HTMLElement>(ids.map(id=>`[data-rule-ids~="${CSS.escape(id)}"]`).join(','));
    const p=all.find(x=>ids.includes(x.id));
    if(!row){revealRules=[];if(!p)return;const topic=ruleTopic(p.definition.rule).name;
      rulesView={...rulesView,query:'',search_options:undefined,status:'',date:'',topics:[],severity:[],who:null,collapsed:rulesView.collapsed.filter(t=>t!==topic),more:[...rulesView.more,topic]};
      revealRules=ids;revealAgain++;if(revealAgain<2)queueMicrotask(()=>void renderPage());else revealRules=[];return;}
    revealRules=[];revealAgain=0;
    let frames=0;
    const show=()=>{if(!row.isConnected){if(++frames<120)requestAnimationFrame(show);return;}
      row.scrollIntoView({block:'center'});row.classList.add('rule-new');row.focus({preventScroll:true});
      window.setTimeout(()=>row.classList.remove('rule-new'),4000);};
    requestAnimationFrame(show);
  }
  const goAttention=()=>{openView('');state.input.value='attention';renderRules();};
  // The Needs attention tab, the section badges and the footer already count what needs attention; the banner only
  // appears for the one action they lack: switching several overtime offers back on at once.
  const banner=switchedOff.length>1&&canPropose?el('div',{class:'rule-banner',role:'status'},
    el('span',{},`${plural(switchedOff.length,'overtime offer')} switched off.`),
    el('span',{class:'rule-banner-actions'},button('Switch overtime back on…',()=>fixOvertime(switchedOff),'secondary small'),button('Review',goAttention,'secondary small'))):null;
  // The count of matching rules is the page's status (footer).
  target.append(footerStatus(summary));
  if(!none){toolbar.root.classList.add('rule-toolbar');target.append(toolbar.root,banner||'',list,legendKey.root,ruleBulk?.bar||'');renderRules();revealProposed();}
}

let reportGrouping='';
async function reportDuties(person:Row,current:unknown){
  const snapshot=(await publicationCache(publicationId,current).data).snapshot as unknown as Row;
  const tz=snapshot.timezone||zone();
  const duties=snapshot.shifts.filter((s:Row)=>!isClosed(s)).flatMap((s:Row)=>s.participants.filter((p:Row)=>p.person_id===person.person_id).map((p:Row)=>({s,p})))
    .filter(({p}:{p:Row})=>!person.period_start||(toLocal(p.start,tz).slice(0,10)<person.period_end&&toLocal(new Date(new Date(p.end).getTime()-1).toISOString(),tz).slice(0,10)>=person.period_start))
    .sort((a:Row,b:Row)=>String(a.p.start).localeCompare(String(b.p.start)));
  // A person's duties: the same docked panel and table as Fairness's drill-down (I-14), dates and times formatted and
  // whole, Clinic · Place under the duty, trainee or supervisor as the shared marks.
  dockPanel({key:'report-duties',title:'Duties · '+person.display_name,width:620,
    sub:person.period_start?`${formatDate(person.period_start,tz)} – ${formatDate(lastIncludedDate(person.period_end),tz)}`:'Whole version',build:({body})=>{
      if(!duties.length){body.append(empty('No duties','Nothing in this version for this period.',null,'calendar'));return;}
      body.append(el('section',{class:'fx-drill'},table(['Day','Time','Duty'],duties.map(({s,p}:{s:Row;p:Row})=>[
        el('span',{class:'nowrap'},formatDay(p.start,tz)),el('span',{class:'mono nowrap'},`${formatTime(p.start,tz)}–${formatTime(p.end,tz)}`),
        el('span',{class:'fx-duty'},el('span',{},s.type_snapshot.name,p.participation==='trainee'?markTag('tr'):p.participation==='supervisor'?markTag('sup'):''),
          el('small',{class:'muted'},[s.service_ids.map((id:string)=>snapshot.services[id]?.name).filter(Boolean).join(', '),snapshot.locations[s.location_id]?.name].filter(Boolean).join(' · ')||'—'))]))));
    }});
}
/** Insights › Workload and › Exports read one published version: the same Publication source sits in the top bar of
 * both tabs (and the breakdown is shared), so switching tabs keeps what you were looking at. */
// The version list is read once per visit to these tabs: choosing a version or a breakdown redraws the page without
// asking for it again (perf PI-9). Any other way in (a tab, a new page) reads it afresh.
let publicationList:{rows:Promise<Row[]>}|null=null,keepPublicationList='';
// Kept for this address only: a redraw that never reaches publicationSource (a cancelled leave, another tab) cannot
// leave a stale list for a later visit.
const redrawReport=()=>{keepPublicationList=location.hash;renderPage();};
async function publicationSource(target:HTMLElement,title:string,description:string){
  if(keepPublicationList!==location.hash||!publicationList)publicationList={rows:get('/api/publications').then(r=>r.publications as Row[])};
  keepPublicationList='';
  const pubs=await publicationList.rows.catch(e=>{publicationList=null;throw e;});
  if(!pubs.length)return null;
  if(!pubs.some(p=>p.id===publicationId))publicationId=pubs[0].id;
  // "<rota name> · 1–31 Oct 2026", with Superseded only when it is (I-15): one term, Version, in the tooltip.
  const month=(d:string)=>formatDate(d.slice(0,8)+'15').replace(/^\d+ /,'');
  const source=select('Version',pubs.map(p=>({value:p.id,group:month(p.starts_on),label:`${p.name||'Version '+p.sequence} · ${spanLabel(p.starts_on+'T00:00:00Z',p.ends_on+'T00:00:00Z','UTC')}${p.current?'':' · Superseded'}`})),publicationId);
  source.input.title='Published version';
  source.input.addEventListener('change',()=>{publicationId=source.input.value;redrawReport();});
  target.append(sectionHead(title,description,source.wrap));
  return pubs.find(p=>p.id===publicationId)!;
}
const breakdownSelect=()=>{const g=select('Report breakdown',[{value:'',label:'Whole publication'},{value:'week',label:'Weekly'},{value:'month',label:'Monthly'}],reportGrouping,false);  // '' (whole publication) is a valid choice
  g.input.addEventListener('change',()=>{reportGrouping=g.input.value;redrawReport();});return g;};
/** Insights › Workload: scheduled hours per person, commitments and resource reservations of one publication. */
async function workloadPage(target:HTMLElement){
  const pub=await publicationSource(target,'Scheduled workload','Scheduled hours only: this report does not record attendance or actual overtime.');
  if(!pub){target.append(nothingPublished(allowed('schedule')?button('Open Schedule',()=>navigate('schedule'),'secondary'):null));return;}
  const grouping=breakdownSelect();
  // A version whose report fails keeps the tabs and the version picker (I-12): say so here, with Retry and a way to the
  // latest version, instead of losing the whole page.
  let report:Row;
  try{report=await get('/api/reports/'+publicationId+'?format=json'+(reportGrouping?'&grouping='+reportGrouping:''));}
  catch(e){if(sessionChanged(e))throw e;
    const latest=(await publicationList?.rows.catch(()=>[] as Row[]))?.[0];
    target.append(el('section',{class:'panel'},readFailed('this version’s report',e),el('div',{class:'actions'},refused(e)?'':button('Retry',redrawReport,'secondary small'),
      latest&&latest.id!==publicationId?button('Show the latest version',()=>{publicationId=latest.id;redrawReport();},'secondary small'):'')));
    return;}
  // Your own single row reads as a line; otherwise the breakdown sits in the workload table's toolbar (it only changes that table).
  const mine=report.rows.length===1&&!reportGrouping&&report.rows[0].person_id===me?.person_id;
  const maxHours=Math.max(1,...report.rows.map((r:any)=>Number(r.scheduled_hours)||0));
  // Hours as a bar against the busiest person, so imbalance shows at a glance.
  const hoursBar=(h:number,max:number)=>{const bar=el('span',{class:'hbar','aria-hidden':'true'},el('span',{}));(bar.firstChild as HTMLElement).style.setProperty('width',Math.round(h/max*100)+'%');return el('span',{class:'hbar-cell'},bar,el('span',{class:'num'},String(h)));};
  target.append(el('section',{class:'panel'},el('div',{class:'panel-head'},el('h2',{class:'panel-title'},'Scheduled workload'),el('span',{class:'muted'},report.overtime_marked_shifts?plural(Number(report.overtime_marked_shifts),'overtime-marked session'):''),report.current?null:badge('Superseded','warn')),
    // Just your own row: say it in a line rather than a one-row table.
    mine?el('p',{class:'workload-mine'},el('strong',{},plural(Number(report.rows[0].assignments),'duty','duties')),' · ',el('strong',{},`${report.rows[0].scheduled_hours} scheduled hours`),Number(report.rows[0].planned_overtime_assignments)?` · ${report.rows[0].planned_overtime_assignments} overtime (${report.rows[0].planned_overtime_hours} h)`:'',Number(report.rows[0].overtime_offered_hours)?` · ${report.rows[0].overtime_offered_hours} h overtime offered`:'',Number(report.rows[0].training_credit)?` · ${report.rows[0].training_credit} educational credits`:''):dataTable<Row>({id:'workload',noun:['person','people'],rows:report.rows.map((r:Row,i:number)=>({...r,_key:String(i)})),rowId:r=>r._key,searchLabel:'Search workload',searchPlaceholder:'Person',
      controls:[grouping.wrap],defaults:{sort:['hours','desc'],hidden:['training','ot_hours','reserved','off_cover']},
      empty:['Nobody in this version','No one has a published duty in this version.'],
      columns:[
        {key:'person',label:'Person',text:r=>String(r.display_name),cell:r=>personSource(r.person_id,personName(r.person_id,String(r.display_name)))},
        ...(reportGrouping?[{key:'from',label:'From',sort:(r:Row)=>String(r.period_start),cell:(r:Row)=>dateUnits([formatDate(r.period_start)])},{key:'through',label:'Through',sort:(r:Row)=>String(r.period_end),cell:(r:Row)=>dateUnits([formatDate(lastIncludedDate(r.period_end))])}]:[]),
        {key:'period',label:'Period',hidden:true,text:r=>r.period_start?`${r.period_start} ${lastIncludedDate(r.period_end)}`:'',facet:r=>r.period_start?`${formatDate(r.period_start)} – ${formatDate(lastIncludedDate(r.period_end))}`:'',cell:r=>r.period_start?dateUnits([formatDate(r.period_start),'– '+formatDate(lastIncludedDate(r.period_end))]):''},
        {key:'assignments',label:TERMS.duties,num:true,sort:r=>Number(r.assignments),cell:r=>r.assignments},
        {key:'hours',label:'Scheduled hours',num:true,sort:r=>Number(r.scheduled_hours),cell:r=>hoursBar(Number(r.scheduled_hours),maxHours)},
        {key:'credits',label:'Workload credits',num:true,hidden:report.rows.every((r:any)=>Number(r.workload_credit)===Number(r.scheduled_hours)),sort:r=>Number(r.workload_credit),cell:r=>r.workload_credit},
        {key:'ot',label:'Overtime duties',num:true,hidden:report.rows.every((r:any)=>!Number(r.planned_overtime_assignments)),sort:r=>Number(r.planned_overtime_assignments),cell:r=>r.planned_overtime_assignments},
        {key:'lead',label:'Team lead turns',num:true,hidden:report.rows.every((r:any)=>!Number(r.lead_turns)),sort:r=>Number(r.lead_turns),cell:r=>r.lead_turns??0},
        // Shown from View › Shown properties.
        {key:'training',label:'Educational credits',num:true,sort:r=>Number(r.training_credit),cell:r=>r.training_credit},
        {key:'ot_hours',label:'Overtime hours',num:true,sort:r=>Number(r.planned_overtime_hours),cell:r=>r.planned_overtime_hours},
        ...(report.rows.some((r:Row)=>r.overtime_offered_hours!=null)?[{key:'ot_offered',label:'Overtime offered (h)',num:true,hidden:report.rows.every((r:Row)=>!Number(r.overtime_offered_hours)),sort:(r:Row)=>Number(r.overtime_offered_hours),cell:(r:Row)=>r.overtime_offered_hours}]:[]),
        ...(report.metric_version!=='scheduled-v1'?[{key:'reserved',label:'Reserved hours',num:true,sort:(r:Row)=>Number(r.reserved_hours),cell:(r:Row)=>r.reserved_hours},{key:'off_cover',label:'Off-cover hours',num:true,sort:(r:Row)=>Number(r.off_cover_hours),cell:(r:Row)=>r.off_cover_hours}]:[]),
      ],onOpen:r=>void reportDuties(r,pub.current),rowButtons:r=>[button('View duties',()=>reportDuties(r,pub.current),'ghost small')]}).root,
    ));
  // Nothing applies: no empty panel.
  const noQuotas=report.quotas&&!report.quotas.rows.length&&report.quotas.complete&&!report.quotas.empty_policy_ids?.length;
  if(report.quotas&&!noQuotas)target.append(quotaPanel(report.quotas));
  if(report.resources?.rows.length)target.append(el('section',{class:'panel'},sectionHead('Configured resource reservations',report.resources.basis),table(['Resource','Sessions','Unit-hours','Peak required units','Capacity at publication'],report.resources.rows.map((r:any)=>[r.name,r.sessions,r.unit_hours,r.peak_required_units,r.capacity_at_publication]))));
  // The footer counts people, not table rows (a weekly breakdown has several rows per person).
  if(!mine&&reportGrouping)target.append(footerStatus(el('span',{class:'muted'},plural(new Set(report.rows.map((r:Row)=>r.person_id)).size,'person','people')+' with duties')));
}
/** Insights › Exports: every download of one publication, a person's calendar file and the calendar subscription.
 * Every row draws at once (I-13); only the Personal calendar row waits for the version's people, and a failure there
 * says so in that row. No report is built until a file is asked for. */
async function exportsPage(target:HTMLElement){
  const pub=await publicationSource(target,'Exports','Files are copies of this version: they do not change when the rota is amended.');
  // Staff mostly want their duties in their own calendar: the subscription comes first for them.
  const subscription=me?.person_id?await subscriptionPanel():null;
  const full=holdsLists(catalogues);
  if(subscription&&!full)target.append(subscription);
  if(!pub)target.append(nothingPublished(allowed('schedule')?button('Open Schedule',()=>navigate('schedule'),'secondary'):null));
  else{
    const grouping=breakdownSelect(),q=()=>reportGrouping?'&grouping='+reportGrouping:'',base='/api/reports/'+pub.id;
    const row=(title:string,what:Child,file:string,url:()=>string,control?:Field<HTMLSelectElement>|null,available=true)=>{
      const go=withIcon(button('Download',()=>download(url(),file),'secondary',{'aria-label':'Download '+title}),'download');
      // A picker with nothing chosen yet: Download waits for it, never the browser's own message (I-30). Nothing is red
      // before anything is chosen (B7 exports): the picker's placeholder says what is needed, and the disabled Download
      // gives the reason to screen readers.
      const why=el('span',{class:'sr-only',id:`export-why-${title.replace(/\W+/g,'-').toLowerCase()}`},'');
      const placeholder=control?.input.required?control.input.querySelector<HTMLOptionElement>('option[value=""]'):null;if(placeholder)placeholder.textContent='Choose a person';
      const ready=()=>{const wait=!available||!!control&&control.input.required&&!control.input.value;go.disabled=wait;
        why.textContent=wait&&available?'Choose a person to download.':'';if(wait&&available)go.setAttribute('aria-describedby',why.id);else go.removeAttribute('aria-describedby');};
      control?.input.addEventListener('change',ready);ready();
      return el('li',{class:'export-row'},el('div',{class:'export-what'},el('strong',{},title),typeof what==='string'?el('small',{class:'muted'},what):what,why),control?.wrap||'',go);
    };
    // One person's duties as a calendar file (a copy); staff get their own, others pick one of the version's people.
    const calendar=(people:Choice[]|null,failed=false)=>{
      if(!full)return row('Personal calendar','Your duties as a calendar file.','personal-rota.ics',()=>`${base}?format=ics&person_id=${me!.person_id}`);
      const person=people?.length?select('Person',people,people.some(p=>p.value===me?.person_id)?me!.person_id!:''):null;
      return row('Personal calendar',failed?couldntLoad('the people in this version'):!people?skeleton('line','Loading the people in this version…'):!people.length?'No people in this version':'One person’s duties as a calendar file.',
        'personal-rota.ics',()=>`${base}?format=ics&person_id=${person!.input.value}`,person,!!person);
    };
    const pending=full||me?.person_id?calendar(null):'';
    // I-20: a staff member (no people list) needs their own calendar only; the department's files are the planners'.
    target.append(el('section',{class:'panel'},el('h2',{class:'panel-title'},'Downloads'),el('ul',{class:'export-list'},
      ...(full?[row('Workload CSV','Duties and hours per person.','scheduled-workload.csv',()=>`${base}?format=csv${q()}`,grouping),
      row('Duties CSV','Every duty, one row per person.','published-assignments.csv',()=>`${base}?format=assignments`),
      // No commitments report for this version: no row, rather than a button that cannot work.
      pub.has_quota_report?row('Commitments CSV','Quotas and commitments per person.','published-commitments.csv',()=>`${base}?format=quotas`):'',
      row('Report JSON','The whole report, for other tools.','publication-report.json',()=>`${base}?format=json${q()}`)]:[]),pending)));
    // Not awaited: the page is shown, and every other download works, while the people load (I-13). A row from a page
    // since replaced is detached, so a late answer changes nothing; a changed session is the shell's to handle.
    if(full&&pending)void publicationCache(pub.id,pub.current).data
      .then(d=>pending.replaceWith(calendar(choices(Object.values(d.snapshot.people||{}),true))))
      .catch(e=>{if(!sessionChanged(e))pending.replaceWith(calendar(null,true));});
  }
  if(subscription&&full)target.append(subscription);
}
/** Subscribe once: a private link your calendar app refreshes, so amendments and swaps appear by themselves.
 *  Two rows once subscribed (I-21): the calendar apps, then the link with Copy and a ⋯ menu; replacing or stopping the
 *  link ends the old one, so both ask with the same-button confirm. */
async function subscriptionPanel(){
  const box=el('section',{class:'panel subscription'});
  // D-057: the ⋯ menu stays open across a redraw of this panel or its page (a live update after the link changed):
  // it moves to the redrawn button and its items are the panel's as drawn now (menuSource); an item closes whichever
  // menu holds it (closePopover), as a redrawn panel never had the menu's own close.
  let items:()=>HTMLElement[]=()=>[];
  menuSource(box,()=>items());
  const draw=async(next?:typeof feedShown)=>{
    if(next!==undefined)feedShown=next;
    const state=await get<Row>('/api/calendar-feed'),path=state.active?feedShown?.path:undefined,recovered=!!feedShown?.recovered;
    // The link is a bearer credential: data-private keeps it out of every feedback picture (CC-01).
    const url=path?location.origin+path:'';
    const link=url?el('input',{type:'text',readonly:true,value:url,class:'feed-url','aria-label':'Your private calendar link','data-private':''}) as HTMLInputElement:null;
    const copy=url?withIcon(button('Copy link',async()=>{try{await navigator.clipboard.writeText(url);notify('Link copied. Add it in your calendar app as a subscription (from URL).','success');}catch{link!.select();}},'secondary'),'copy'):null;
    // One tap per calendar app: webcal opens Apple Calendar or the Outlook app; Google and Outlook on the web take the link as a parameter.
    const webcal=url.replace(/^https?:/,'webcal:');
    const apps=url?el('div',{class:'subscription-apps'},el('span',{class:'muted'},'Add to'),
      el('a',{class:'button primary',href:webcal},'Apple or Outlook app'),
      el('a',{class:'button secondary',href:'https://calendar.google.com/calendar/r?cid='+encodeURIComponent(webcal),target:'_blank',rel:'noopener noreferrer'},'Google Calendar'),
      el('a',{class:'button secondary',href:`https://outlook.office.com/calendar/0/addfromweb?url=${encodeURIComponent(url)}&name=Rota`,target:'_blank',rel:'noopener noreferrer'},'Outlook on the web')):null;
    // Focus follows the change: the new link, or Subscribe once sharing stopped (not dropped to the page).
    const settle=()=>document.querySelector<HTMLElement>('#main .subscription')?.querySelector<HTMLElement>('.feed-url, .subscription .button.primary:not(a), [aria-label="More link actions"]')?.focus();
    const renew=()=>command<Row>('calendar_feed.create',{}).then(r=>(box.isConnected||!feedDraw?draw:feedDraw)({path:r.path,recovered:!r.path})).then(settle);
    const more=state.active?opens(el('button',{type:'button',class:'icon-button','aria-label':'More link actions','data-tip':'More link actions'},icon('dots')),'menu'):null;
    // Each item carries its consequence as help, so they are built here (menuItem) and handed to the shared menu.
    items=()=>[
      armed(menuItem('New link','reset',()=>{closePopover();return renew();},{'data-tip':'The current link stops working.'})),
      armed(menuItem('Stop sharing','close',async()=>{closePopover();await command('calendar_feed.revoke',{});await (box.isConnected||!feedDraw?draw:feedDraw)(null);settle();notify('Calendar link stopped.');},{'data-tip':'The link stops working; your calendar app keeps nothing new.'}))];
    more?.addEventListener('click',()=>{menu(more,'More link actions',items,{placement:'below-end',keep:'.subscription [aria-label="More link actions"]'});});
    const subscribe=state.active?null:button('Subscribe in my calendar',renew,'primary');
    box.replaceChildren(el('div',{class:'panel-row'},el('div',{},el('h2',{class:'panel-title'},'Calendar subscription'),
      el('small',{class:'muted'},url?'Shown once. Your calendar app fetches updates on its own schedule (minutes to a day).'
        :recovered&&state.active?'Your previous link cannot be shown again. Choose ⋯ › New link to replace it.'
        :state.active?`Subscribed${state.last_used_at?' · last refreshed '+formatDate(state.last_used_at,zone()):''}.`
        :'')),
      el('span',{class:'grow'}),...(link?[]:[subscribe||'',more||''])),
      apps||'',link?el('div',{class:'subscription-link'},link,copy!,more!):'');
  };
  feedDraw=draw;await draw();return box;
}

/** A source file's yes/no column as words ("true", "1" or "yes" is Yes; empty or anything else is No). */
const yesNo=(v:unknown)=>/^(true|yes|y|1)$/i.test(String(v??'').trim())?'Yes':'No';
async function importMapping(batchId:string){
  const batch=(await get('/api/imports/'+batchId)).batch;
  if(batch.status!=='preview'){showDialog('Import source evidence',batch.source_name,body=>body.append(note('This source batch was already saved into a draft. The reviewed matches for each imported row are preserved.'),table(['Source row','Type','Location','Service','Person','Team lead','Overtime','Source note','Status'],batch.rows.map((r:any)=>[r.row_number,r.data.assignment_type,r.data.location,r.data.service,r.data.person,yesNo(r.data.team_lead),yesNo(r.data.overtime),r.data.source_note||'',statusBadge(r.shift_id?'current':'',r.shift_id?'Mapped':'Not mapped')]))));return;}
  const w=(await rotaMeta(batch.workspace_id)).workspace;
  if(batch.workspace_revision!==w.revision){confirmAction('Refresh reconciliation preview','The draft changed since this preview. Preserve the original source batch, then reconfirm every mapping against the current draft.',async reason=>{await command('import.refresh',{workspace_id:w.id,batch_id:batch.id,reason},undefined,w.revision);await refresh();notify('Preview refreshed. Open Review row mappings again to reconfirm every row.');},'Confirm',undefined,true);return;}
  formDialog('Reconcile import identities','Every source row requires explicit reviewed catalogue selections. Suggested unique label matches are not clinical grants or approvals.',form=>{
    const mapping:Record<string,HTMLSelectElement>[]=[];
    const sharedClinics:(()=>string[])[]=[];
    const exact=(kind:string,label:string)=>{const matches=rows(kind).filter(r=>labelOf(r)===label);return matches.length===1?matches[0].id:'';};
    // Choosing a match once fills every other row with the same source text that is still unmatched (each stays reviewable).
    const same:{key:string;text:string;input:HTMLSelectElement}[]=[];
    form.addEventListener('change',e=>{
      const from=same.find(x=>x.input===e.target);if(!from||!from.input.value||!from.text)return;
      for(const x of same)if(x!==from&&x.key===from.key&&x.text===from.text&&!x.input.value)x.input.value=from.input.value;
    });
    for(const r of batch.rows){
      const fields:Record<string,HTMLSelectElement>={};
      const line=el('fieldset',{class:'span-2 multi-field'},el('legend',{},`Source row ${r.row_number}: ${r.data.person||'Unknown person'}`),el('p',{class:'muted'},`${r.data.start} → ${r.data.end} · planned overtime: ${r.data.overtime}`));
      if(r.data.team_lead)line.append(note(`Team lead: ${r.data.team_lead.toLowerCase()==='true'?'Yes':'No'}`));
      if(r.data.source_note)line.append(note(`Source note: ${r.data.source_note}`,'warning'));
      if(r.errors?.length)line.append(note(r.errors.map((e:any)=>typeof e==='string'?e:e.message).join('; '),'error'));
      const grid=el('div',{class:'form-grid'});
      for(const [key,kind,source] of [['assignment_type_id','assignment_types','assignment_type'],['location_id','locations','location'],['service_id','services','service'],['person_id','people','person']]){
        const f=select(`Row ${r.row_number} ${source.replaceAll('_',' ')}`,[{value:'',label:`Choose a reviewed match for “${r.data[source]||''}”`},...choices(rows(kind))],exact(kind,r.data[source]));fields[key]=f.input;grid.append(f.wrap);same.push({key,text:String(r.data[source]||'').trim().toLowerCase(),input:f.input});
      }
      const extra=multi(`Row ${r.row_number} additional clinics covered by this duty`,choices(rows('services')));
      line.append(grid,el('p',{},el('strong',{},'Shared clinic coverage')),note('Select additional clinics only when the same duty covers them. The duty type must allow shared coverage.'),extra.wrap);
      form.append(line);mapping.push(fields);sharedClinics.push(extra.values);
    }
    const reason=area('Reconciliation reason');reason.input.required=true;reason.input.minLength=3;form.append(reason.wrap);
    return async()=>{await command('import.apply',{workspace_id:batch.workspace_id,batch_id:batch.id,mappings:batch.rows.map((r:any,i:number)=>({row_number:r.row_number,...Object.fromEntries(Object.entries(mapping[i]).map(([k,v])=>[k,v.value])),additional_service_ids:sharedClinics[i]()})),reason:reason.input.value},undefined,w.revision);notify('Reconciled source saved as a draft and revalidated. It is not published.');await refresh();};
  },'Save reconciled draft');
}

let importTab='staff';
async function dataPage(target:HTMLElement){
  // Staff and session imports load together; session imports go into draft rotas, so they are for coordinators only.
  const staff=el('div',{});
  const [staffBatches,sessions]=await Promise.all([staffImportSection(staff,ctx()),permits(CAPABILITY.COORDINATOR)?Promise.all([get('/api/workspaces'),get('/api/imports'),get('/api/reference-imports')]):null]);
  // The footer says what waits for someone, across every kind of import.
  const waiting=staffBatches.filter(b=>['preview','partly_applied'].includes(b.status)).length+(sessions?(sessions[1].batches as Row[]).filter(b=>b.status==='preview').length:0);
  const foot=footerStatus(el('span',{class:'muted'},waiting?plural(waiting,'import')+' to review':'No import waiting for review'));
  // Staff imports only (credentialers): its header is the page's, so it moves into the top bar.
  if(!sessions){target.append(...staff.childNodes,foot);return;}
  const [work,batchData,referenceData]=sessions;
  const drafts:Row[]=work.workspaces.filter((w:Row)=>w.status==='draft');
  const baseline:Node[]=[];
  for(const source of referenceData.imports){
    const labels:Record<string,string>={imported:'Imported',historical_evidence:'Retained evidence',unresolved:'Needs reconciliation'};
    // Grouped by outcome: what needs reconciliation comes first.
    const records=dataTable<Row>({id:'source-records-'+source.id,noun:['record','records'],rows:source.records.map((r:Row,i:number)=>({...r,_key:String(i)})),rowId:r=>r._key,pageSize:100,
      searchLabel:'Find source record',searchPlaceholder:'Record, source or details',defaults:{group:'status'},empty:['No source records',''],
      columns:[
        {key:'record',label:'Record',text:r=>String(r.label),cell:r=>r.label},
        {key:'status',label:'Outcome',options:Object.values(labels),facet:r=>labels[r.outcome]||String(r.outcome),cell:r=>statusBadge(r.outcome==='imported'?'applied':r.outcome==='unresolved'?'attention':'',labels[r.outcome]||String(r.outcome))},
        {key:'details',label:'Details',text:r=>String(r.reason||''),cell:r=>r.reason},
        {key:'source',label:'Source',text:r=>`${r.path} ${r.source_key}`,cell:r=>el('small',{class:'mono muted'},`${r.path} · ${r.source_key}`)},
      ]});
    const previewDuties=()=>formDialog('Preview dated source duties','Select a draft covering the source dates. Review employee, duty type, clinic and location matches before saving.',form=>{
      const workspace=select('Destination draft',choices(drafts),drafts.some(d=>d.id===workspaceId)?workspaceId:'');form.append(workspace.wrap);
      return async()=>{const w=(await rotaMeta(workspace.input.value)).workspace;const result=await command('import.reference_preview',{workspace_id:w.id,reference_import_id:source.id},undefined,w.revision);await refresh();await importMapping(result.batch.id);};
    },'Preview source duties');
    baseline.push(el('section',{class:'panel'},sectionHead('Baseline reconciliation',`${source.counts.imported} imported · ${source.counts.historical_evidence} retained as evidence · ${source.counts.unresolved} need reconciliation`,button('Preview dated source duties',previewDuties,'primary',{disabled:!drafts.length,why:TERMS.needsDraft})),
      note('Records needing reconciliation are not active scheduling configuration. Imported records may still have source details awaiting review.'),
      ...(source.source_updates.length?[el('h3',{},'Source dates'),table(['Dataset','Source reports updated'],source.source_updates.map((r:Row)=>[r.label,r.updated]))]:[]),
      records.root));
  }
  const newImport=()=>formDialog('Preview CSV source','Upload a UTF-8 CSV file, then review every identity match before saving a draft.',form=>{
    const workspace=select('Destination draft',choices(drafts),drafts.some(d=>d.id===workspaceId)?workspaceId:''),source=field('Source name','text','',{required:true,maxlength:160}),file=field('CSV file','file','',{required:true,accept:'.csv,text/csv'});
    form.append(workspace.wrap,source.wrap,file.wrap,el('div',{class:'span-2'},el('strong',{},'Required columns'),el('ul',{class:'mono'},...['assignment_type','location','service','person','start','end','overtime'].map(c=>el('li',{},c))),
      el('ul',{},el('li',{},'start and end: ISO timestamps with the UTC offset'),el('li',{},'overtime: true or false'),el('li',{},'Optional typed fields: field_<key>'),el('li',{},'Up to 1 MB and 1,000 data rows'))));
    return async()=>{const f=file.input.files?.[0];if(!f)throw new Error('Select a CSV source.');if(f.size>1_000_000)throw new Error('This source exceeds the 1 MB limit.');const w=(await rotaMeta(workspace.input.value)).workspace;const result=await command('import.preview',{workspace_id:w.id,source_name:source.input.value,csv_text:await f.text()},undefined,w.revision);await refresh();await importMapping(result.batch.id);};
  },'Parse and preview');
  // One tab per kind of import, each with its own header and upload.
  const sessionTable=()=>el('div',{},sectionHead('Session imports','',button('Upload CSV source',newImport,'primary',{disabled:!drafts.length,why:TERMS.needsDraft})),el('section',{class:'panel'},dataTable<Row>({id:'imports',noun:['import','imports'],rows:batchData.batches,rowId:r=>r.id,searchLabel:'Search imports',searchPlaceholder:'Source name',
    empty:['No imports yet',drafts.length?'Upload a CSV file to preview its rows against a draft.':'Create a draft rota first, then upload a CSV file.'],defaults:{sort:['created','desc']},
    columns:[
      {key:'source',label:'Source',text:r=>String(r.source_name),cell:r=>el('strong',{},r.source_name)},
      {key:'state',label:'State',facet:r=>String(r.status).replaceAll('_',' '),cell:r=>status(r.status)},
      {key:'created',label:'Created',sort:r=>String(r.created_at),cell:r=>dateUnits([formatDate(r.created_at,zone())])},
    ],
    rowButtons:r=>[button(r.status==='preview'?'Review row mappings':'Inspect source evidence',()=>importMapping(r.id),'secondary small')]}).root));
  // A choice inside the page's own tabs reads lighter (an underline), never a second tab bar of the same weight (SY-03).
  const kinds=tabs('Imports',[['staff','Staff',()=>staff],['sessions','Rota sessions',sessionTable],...(baseline.length?[['baseline','Baseline',()=>el('div',{},...baseline)] as [string,string,()=>Node]]:[])],importTab,k=>{importTab=k;});
  kinds.classList.add('tabs-inner');target.append(foot,kinds);
}

/** A run's duration as people read it ("2 min 4 s", "55 min"): the one format, dom.ts duration reviewed. */
const runSeconds=duration;
const jobSeconds=(a:string,b:string)=>runSeconds(Math.max(0,(Date.parse(b)-Date.parse(a))/1000));
function jobTiming(job:Row):string{
  const seconds=jobSeconds;  // one format with the panel and progress lines: "< 1 s", "14.6 s"
  const parts:string[]=[];
  if(job.started_at)parts.push(`${job.attempts>1?'Before latest attempt':'Waiting'}: ${seconds(job.created_at,job.started_at)}`);
  if(job.started_at&&job.completed_at)parts.push(`${job.attempts>1?'Latest attempt':'Scheduling'}: ${seconds(job.started_at,job.completed_at)}`);
  if(job.completed_at)parts.push(`Total: ${seconds(job.created_at,job.completed_at)}`);
  if(job.attempts>1)parts.push(`${job.attempts} attempts`);
  return parts.join(' · ')||'Timing not yet available';
}

/** A moment as people read it: "5 Oct 2026 20:00" (not an ISO timestamp). */
const stamp=(iso:string)=>`${formatDate(iso,zone())} ${formatTime(iso,zone())}`;
const STALE_WORDS:Record<Currency,string>={'':'',rota:'The rota changed since this run. Run auto-schedule again to get a result you can apply.',
  scheduler:'The scheduler changed since this run. Run auto-schedule again to get a result you can apply.',
  unknown:'This older run cannot be confirmed as current, so it cannot be applied. Run auto-schedule again to get a result you can apply.',
  superseded:'A newer run replaced this one. Review the latest run.'};
/** Apply refused because a newer run replaced this one (RUN_SUPERSEDED): said once, with the way to the latest run; the
 *  refusal itself has nothing more to report. Any other error goes on as it was. `here` says it in the open results
 *  dialog instead, so the press that was refused never just closes it. */
function supersededRun(e:unknown,workspace:string,here?:()=>void):never{
  if(!(e instanceof ApiError&&e.code===RUN_SUPERSEDED))throw e;
  if(here)here();else{closeDialogs();notify(STALE_WORDS.superseded,'warning',{label:SUPERSEDED.next,run:()=>reviewLatestRun(workspace)});}
  void refresh();
  throw new ApiError('PAGE_LEFT','',0);
}
/** A stored proposal can be reviewed at any time; only a current candidate can change a draft. */
// A proposal the rota changed under stays reviewable when the server re-checks it on Apply (maintenance currency
// revalidation_required): Apply then keeps only what still fits (A4), never discarding the whole run.
const currentGeneration=(job:Row,data:WorkspaceData)=>data.workspace.status==='draft'&&!data.workspace.archived_at&&job.kind==='generation'&&job.status==='succeeded'&&(!currency(job,data.current_fingerprint)||rechecked(job))&&
  ['candidate_feasible','candidate_with_gaps','unknown_time_limit'].includes(job.result?.status)&&!!job.result?.validation&&!dismissedJobs.has(job.id);
/** "Sun 4 Oct", the part of the day and the hours of a session: how people name a clinic session. */
const dayName=(iso:string)=>formatDay(iso,zone());
const hours=(a:string,b:string)=>`${formatTime(a,zone())}–${formatTime(b,zone())}`;
/** The result in three numbers: the score once applied (summary.score_saved, uncapped: the footer's and Insights' number
 *  after Apply) with its change from the saved rota now, blocking problems with theirs, and how many changes. The change
 *  is read from both stored scores (scoreDelta): the server's score_delta is the same uncapped difference now, but
 *  results stored before it was uncapped carry a capped (or no) score_delta, and results without a score_before have none. */
function resultMetrics(s:Row,changes:number,blocking:number,current?:Row,strict=true):HTMLElement{
  // Without a score of the run's own, the saved rota's is said as "Score now" (A-03), never as the run's; Must fix is
  // the candidate's own (its validation), never the rota's.
  const own=s.score_saved??s.score,before=s.score_before;
  const blockedAfter=Number(own?.blocking_count??blocking),blockedBefore=before?.blocking_count;
  return el('div',{class:'auto-stats',role:'group','aria-label':'Result'},
    scoreTile(s,current,strict),
    statTile('Must fix',String(blockedAfter),blockedBefore!=null&&blockedBefore!==blockedAfter?statChange(blockedAfter-blockedBefore,blockedAfter<blockedBefore,String(blockedBefore)):'',blockedAfter?'err':''),
    statTile('Changes',String(changes),''));
}
// A removal no problem caused (packages/domain/cp_sat.py): the person was moved to balance the work.
const REBALANCED='Rebalanced automatic proposal';
type ChangeRow={who:string;pid?:string;what:string;when:string;why:string};
/** Why a person is taken off, in the planner's words: the draft's own finding for them on that session, else the run's. */
function removalWhy(x:Row,findings:Row[]):string{
  const f=findings.find(y=>y.severity==='error'&&y.shift_id===x.shift_id&&y.person_id===x.person_id),said=String(x.reason||'');
  return f?String(f.message):/unavailab|rotation/i.test(said)?leaveLabel():said===REBALANCED?'To balance the work':said;
}
/** Changes as "What changes" lists them (LIVE-24): a person taken off and put back on the same session with changed
 *  details is one change, not two. */
function changeCount(r:Row):number{
  const on=new Set(((r.additions||[]) as Row[]).map(a=>a.shift_id+'|'+a.participant.person_id));
  return (r.additions||[]).length+((r.removals||[]) as Row[]).filter(x=>!on.has(x.shift_id+'|'+x.person_id)).length;
}
/** What a proposal changes, in a few groups: placements added; people taken off leave or a broken rule; people taken off to
 *  balance the work; the same person with changed details. Why someone is taken off is the draft's own finding for them on
 *  that session (plain words, by code); the run's own words only when the draft has none. */
function changeGroups(r:Row,findings:Row[]):{label:string;rows:ChangeRow[];why:boolean}[]{
  const key=(sid:string,pid:string)=>sid+'|'+pid,removals:Row[]=r.removals||[],additions:Row[]=r.additions||[];
  const off=new Set(removals.map(x=>key(x.shift_id,x.person_id))),on=new Set(additions.map(a=>key(a.shift_id,a.participant.person_id)));
  const cause=new Map<string,Row>();
  for(const f of findings)if(f.severity==='error'&&f.shift_id&&f.person_id&&!cause.has(key(f.shift_id,f.person_id)))cause.set(key(f.shift_id,f.person_id),f);
  const row=(x:Row,pid:string,start:string,end:string,why=''):ChangeRow=>({who:name('people',pid),pid,
    what:[...(x.service_names||[]),x.session_label].filter(Boolean).join(' · ')||'Session',when:`${dayName(start)} · ${hours(start,end)}`,why});
  const leave:ChangeRow[]=[],rule:ChangeRow[]=[],balance:ChangeRow[]=[];
  for(const x of removals){
    if(on.has(key(x.shift_id,x.person_id)))continue;
    const f=cause.get(key(x.shift_id,x.person_id)),said=String(x.reason||'');
    // shortcut: without a draft finding, leave is known only from the run's text ("Known unavailability…"); removals that
    // carry their finding codes would replace this test.
    if(f?['UNAVAILABLE','EXTERNAL_ROTATION'].includes(f.code):/unavailab|rotation/i.test(said))leave.push(row(x,x.person_id,x.start,x.end,f?String(f.message):leaveLabel()));
    else if(f||said&&said!==REBALANCED)rule.push(row(x,x.person_id,x.start,x.end,f?String(f.message):said));
    else balance.push(row(x,x.person_id,x.start,x.end));
  }
  const added=(changed:boolean)=>additions.filter(a=>off.has(key(a.shift_id,a.participant.person_id))===changed).map(a=>row(a,a.participant.person_id,a.participant.start,a.participant.end));
  return [{label:'New placements',rows:added(false),why:false},{label:'Taken off: on leave or away',rows:leave,why:true},
    {label:'Taken off: breaks a rule',rows:rule,why:true},{label:'Taken off to balance the work',rows:balance,why:false},
    {label:'Same person, changed details',rows:added(true),why:false}].filter(g=>g.rows.length);
}
/** "What changes": every change, visible before Apply, in one shared collection grouped by kind with its count; each
 *  group shows its first rows and "Show N more" (searchable, sortable), so the person deciding can see them all here. */
function changesSection(groups:ReturnType<typeof changeGroups>):HTMLElement|string{
  if(!groups.length)return '';
  type Line=ChangeRow&{_key:string;change:string};
  const rows:Line[]=groups.flatMap(g=>g.rows.map((x,i)=>({...x,change:g.label,_key:g.label+'|'+i}))),why=groups.some(g=>g.why);
  return el('section',{class:'auto-changes','aria-label':'What changes'},el('h3',{},`What changes · ${rows.length}`),dataTable<Line>({id:'auto-changes',noun:['change','changes'],rows,rowId:x=>x._key,pageSize:Math.max(rows.length,1),groupLimit:10,
    defaults:{group:'change'},searchLabel:'Search changes',searchPlaceholder:'Person or session',
    columns:[
      {key:'change',label:'Change',hidden:true,text:x=>x.change,facet:x=>x.change,ordered:groups.map(g=>g.label),cell:x=>x.change},
      {key:'who',label:'Person',text:x=>x.who,facet:x=>x.who,cell:x=>el('strong',{},x.pid?nameText(x.pid,x.who):x.who)},
      {key:'what',label:'Session',text:x=>x.what+' '+x.when,cell:x=>el('span',{class:'auto-session'},x.what,el('small',{},x.when))},
      ...(why?[{key:'why',label:'Why',text:(x:Line)=>x.why,cell:(x:Line)=>x.why||'—'}]:[]),
    ]}).root);
}
/** What a finished generation produced (job.outcome, apps/api/serialization.py job_outcome): label and badge tone. */
const RUN_OUTCOME:Record<string,[string,Tone|'']>={complete:['Ready for review','ok'],partial:['Gaps remain','warn'],
  needs_more_time:['More search time needed',''],infeasible:['Places cannot be filled','err'],failed:['Could not finish','err'],cancelled:['Cancelled','']};
// Truthful about an early stop (SP-04): saved inputs are always kept; only preparation that had finished (and has not
// expired) is reused, so a run stopped while preparing prepares again.
const GENERATION_RESUME_NOTE='Saved inputs are kept. Preparation that finished, and the best hints found, are reused when you continue; preparation stopped early runs again.';
/** What to do after a run stopped on something running it again cannot fix (job.recovery, CA-07). The server names the
 *  cause, the roles that fix it and the link for this account; nothing is guessed here. */
function recoveryNote(r:JobRecovery):HTMLElement{
  const who=r.owner_roles.length?(permits(...r.owner_roles)?'You can fix this.':`Who can fix it: ${r.owner_roles.map(roleLabel).join(' or ')}.`):'';
  const go=r.link_target?r.link_target.replace(/^#/,''):'',page=go.split(/[/?]/)[0];
  if(r.action==='review_rules')return el('div',{class:'run-recovery'},note(`Auto-schedule cannot use some active rules as they are. Running it again gives the same result until they change. ${who}`,'warning'),
    go&&allowed(page)?el('div',{class:'actions'},withIcon(button('Open Rules',()=>navigate(go),'secondary small'),'rules')):'');
  // An implementation fault: reported, never retried as if it might pass.
  return note(`Auto-schedule stopped on an internal fault (${r.cause_code}). Running it again gives the same result until it is fixed. ${who}`,'error',new ApiError(r.cause_code,'',500));
}
// Reasons only a credentialer can change (clinic eligibility, rank, skills): the review names who to ask.
const ELIGIBILITY_CODES=new Set(['ELIGIBILITY_MISSING','MEMBERSHIP_MISSING','RANK_NOT_ALLOWED','CAPABILITY_NOT_GRANTED','INSUFFICIENT_CAPABILITY_CANDIDATES']);
/** One reason a place stayed open, as the summary names it (summary.top_reasons). */
const reasonLine=(x:Row)=>{const m=String(x.message||'');
  return x.kind==='rule'?`Rule: ${m}`:x.kind==='person'&&x.person_id?`${name('people',x.person_id)}: ${m}`:m.charAt(0).toUpperCase()+m.slice(1);};
/** A run that stopped because some places cannot be filled (summary.open_places_count, proven_open_places, top_reasons):
 *  what happened in one sentence, `next` (what to do), then the main reasons with their session counts. Only proven
 *  shortages are called impossible; the rest simply remain open. */
function shortfallBlock(r:Row,next:string,limit=3):HTMLElement{
  const s:Row=r.summary||{},places=Number(s.open_places_count||0),sessions=Number(s.sessions_affected||0);
  const reasons=rankedReasons(s),proven=Math.min(places,Number(s.proven_open_places||0));
  const title=!places?'Some places cannot be filled':proven>=places?`${plural(places,'place')} cannot be filled with the current people and rules`
    :`${plural(places,'place')} could not be filled${proven?` · ${proven} of them cannot be with the current people and rules`:''}`;
  return el('div',{class:'shortfall'},outcome('err',title,[sessions?`${plural(sessions,'session')} affected.`:'',next].filter(Boolean).join(' ')),
    reasons.length?el('ul',{class:'shortfall-reasons','aria-label':'Main reasons'},...reasons.slice(0,limit).map(x=>el('li',{},reasonLine(x),
      x.session_count>1?el('span',{class:'muted'},` · ${plural(x.session_count,'session')}`):''))):'',
    reasons.length>limit?el('p',{class:'muted'},`${plural(reasons.length-limit,'more reason')} in Review latest result.`):'',excludedLine(s));
}
/** The server's reasons for open places, in its order (summary.top_reasons: proven shortages first, then by unfilled
 *  places); reasons that read the same are one line with the largest count. */
function rankedReasons(s:Row):Row[]{
  const byLine=new Map<string,Row>();for(const x of (s.top_reasons||[]) as Row[]){const k=reasonLine(x),seen=byLine.get(k);if(!seen||Number(x.session_count)>Number(seen.session_count))byLine.set(k,x);}
  return [...byLine.values()];
}
/** People left out of the whole run (inactive, no department membership), said once (summary.excluded_people). */
const excludedLine=(s:Row)=>(s.excluded_people||[]).length?el('p',{class:'muted auto-excluded'},(s.excluded_people as Row[]).map(x=>x.message).join(' · ')):'';
// The per-place wording of those exclusions (packages/domain/solver_summary.py NOT_USABLE).
const EXCLUDED_LABEL:Record<string,string>={PERSON_INACTIVE:'marked inactive',MEMBERSHIP_MISSING:'without current department membership'};
/** Needs you: the run's main reasons above the cards, as the server ranks them, and who it left out once. */
function mainReasons(s:Row,limit=3):HTMLElement|string{
  const reasons=rankedReasons(s);if(!reasons.length&&!(s.excluded_people||[]).length)return '';
  return el('div',{class:'auto-reasons'},reasons.length?el('ul',{class:'shortfall-reasons','aria-label':'Main reasons'},...reasons.slice(0,limit).map(x=>el('li',{},reasonLine(x),
    x.session_count>1?el('span',{class:'muted'},` · ${plural(x.session_count,'session')}`):''))):'',excludedLine(s));
}
/** A finished proposal, said the same way in the panel and the Results dialog: what happened and what is needed from you,
 *  three numbers, what changes (the panel; the dialog lists every change), then one card per place that needs you with its
 *  one main reason and one fix, and advisory notes as one line. Nothing changes until Apply (undoable). */
function proposalSummary(data:WorkspaceData|null,job:Row,o:{editable:boolean;optimality?:string;/** strictlyCurrent(job): only then does the run compare itself with the rota now. */strict?:boolean;panel:boolean;close?:()=>void}):HTMLElement{
  const r=job.result,s:Row=r.summary||{},byId=new Map((data?.shifts||[]).map(x=>[x.id,x]));
  const findings:Row[]=r.validation?.findings||[];
  const accepted=new Set((data?.workspace.accepted_issues||[]).map((a:Row)=>a.key));
  const advisory=findings.filter(f=>f.severity!=='error'&&!accepted.has(issueKey(f)));
  const places:Row[]=s.open_places||[],openCount:number=s.open_place_count??places.length;
  // Places, not sessions (one session may be short by several people): the proposal's rota summary; results stored
  // before it existed carry the same totals flat.
  const sum:Row=s.rota_summary||{places_needed:s.required_places,places_filled:s.filled_places,places_open:(s.required_places??0)-(s.filled_places??0)};
  const openPlaces=Number(sum.places_open||0),filled=Number(sum.places_filled??0),needed=Number(sum.places_needed??0);
  // A session short of people is one open-place card; its coverage findings are not counted again as problems.
  const gaps=new Set(places.map(p=>p.shift_id));
  if(openCount>places.length)for(const c of r.validation?.coverage||[])if(c.status==='gap')gaps.add(c.shift_id);
  // For the cards only: Blocking (resultMetrics) counts every error of the proposal's validation.
  const mandatory=findings.filter(f=>f.severity==='error'&&!(f.shift_id&&!f.person_id&&gaps.has(f.shift_id)));
  const changes=changeCount(r),close=()=>o.close?.();
  // Already in the draft, changed or new: the draft's own check against the proposal's, only when both were made from
  // the rota's current inputs (findings.ts draftComparison); otherwise nothing is said.
  const compare=data?draftComparison(data.workspace.validation,data.current_fingerprint,notCompared(job)?null:job.input_fingerprint):null;
  // Show on schedule: the proposal is drawn on the grid, so the session is seen as it would be.
  const show=(shift:Row|undefined)=>shift&&data?button('Show on schedule',()=>{close();proposal={job_id:job.id,workspace_id:job.workspace_id,additions:r.additions||[],removals:r.removals||[],job,workspace_revision:data.workspace.revision,session:sessionKey()};showOnSchedule(shift);},'secondary small'):null;
  // Use X: someone no rule stops (the schedule needed them elsewhere). Applies the proposal, checks X for the session
  // with the fill panel's own check, then adds X as an ordinary draft edit (Undo takes X off again).
  const use=(p:Row,shift:Row|undefined)=>{const free=(p.people||[]).find((x:Row)=>x.kind==='FREE');if(!o.panel||!o.editable||!shift||shift.locked||!free)return null;const who=name('people',free.person_id);
    return button(`Apply, then add ${who}`,async()=>{await applyJob(job);const w=(await rotaMeta(job.workspace_id)).workspace,now=(await rotaWhole(w.id)).shifts.find(x=>x.id===shift.id)!;
      const options=await fillOptions({workspace_id:w.id,shift_id:shift.id,expected_revision:w.revision});
      const c=options.candidates.find((x:Row)=>x.person_id===free.person_id);
      if(!c||c.status==='blocked'){await refresh();throw new Error(`The suggestions are applied, but ${who} can no longer take this session. Pick someone else.`);}
      // The edit's own toast carries Undo (it takes X off; the applied proposal stays).
      await assignCandidate(w,now,c,options);await refresh();},'secondary small',{'data-tip':`Applies the ${plural(changes,'change')} first, then adds ${who} to this session`});};
  // A mandatory rule stopped someone: ask for a separately approved exception for them (only rules that allow one).
  const exception=(p:Row)=>{const x=(p.people||[]).find((y:Row)=>y.rule_id&&allowsException(find('policies',y.rule_id)));
    return o.editable&&x?exceptionButton(x.rule_id,x.person_id,p.start):null;};
  const people=(label:string,filters:Record<string,string[]>)=>allowed('people')?button(label,()=>{close();openFiltered('people',filters);navigate('people');},'secondary small'):null;
  const credentialed=permits(CAPABILITY.CREDENTIALER);
  // The rule most of them are at the limit of (people[].rule_id, the server's): opened where changes are proposed and
  // approved, when no exception is allowed. Nobody's limit is raised from here.
  const limitRule=(p:Row)=>{const n=new Map<string,number>();for(const x of (p.people||[]) as Row[])if(x.rule_id)n.set(x.rule_id,(n.get(x.rule_id)||0)+1);
    const id=[...n].sort((a,b)=>b[1]-a[1])[0]?.[0],rule=id?find('policies',id)?.name:'';
    return id&&rule?button(`Open ${rule}`,()=>{close();void openRuleSource(id).catch(reportError);},'secondary small'):null;};
  const fix=(p:Row,shift:Row|undefined,kind:PlaceFix)=>kind==='free'?use(p,shift):kind==='exception'?exception(p)||limitRule(p)
    :kind==='eligibility'?people(credentialed?'Give eligibility':'Open People',{status:['Active']}):kind==='skill'?people(credentialed?'Grant a skill':'Open People',{status:['Active']})
    :kind==='rank'?people('Open People',{status:['Active']}):kind==='inactive'?people('Open inactive people',{status:['Inactive']})
    :kind==='rule'?(typedRule(p)?button(`Open ${name('policies',typedRule(p)!)}`,()=>{close();void openRuleSource(typedRule(p)!).catch(reportError);},'secondary small')
      :button('Open rules',()=>{close();navigate('rules');},'secondary small')):null;
  // Why nobody can work it, from the server's typed counts (solver_summary exclusion_counts: distinct people per reason;
  // reasons overlap, so the largest decides and nothing is added up). Someone free or at a limit stays the first fix.
  const TYPED_FIX:Partial<Record<SolverExclusionCount['code'],PlaceFix>>={ELIGIBILITY_MISSING:'eligibility',ELIGIBILITY_ENDED:'eligibility',CAPABILITY_NOT_GRANTED:'skill',
    MEMBERSHIP_MISSING:'rank',RANK_NOT_ALLOWED:'rank',PERSON_INACTIVE:'inactive',RESTRICTION:'rule'};
  const topExclusion=(p:Row)=>[...((p.exclusion_counts||[]) as SolverExclusionCount[])].sort((a,b)=>b.people-a.people)[0];
  const typedRule=(p:Row)=>{const t=topExclusion(p);return t?.code==='RESTRICTION'&&t.rule_id?t.rule_id:undefined;};
  const placeFix=(p:Row):PlaceFix=>{const said=placeReason(p,id=>name('people',id)).fix,t=topExclusion(p);
    return said==='free'||said==='exception'||!t?said:TYPED_FIX[t.code]||said;};
  const leftOut=((s.excluded_people||[]) as Row[]).map(x=>EXCLUDED_LABEL[x.code]).filter(Boolean);
  const cards=places.map(p=>{const shift=byId.get(p.shift_id),kind=placeFix(p);
    // The duty's own time (its clock in the department's zone), never a fixed part of the day.
    const issue=openPlaceIssue(p,id=>name('people',id),`${(p.service_names||[]).join(' + ')||p.session_label} · ${dayName(p.start)} · ${hours(p.start,p.end)}`,
      `${p.session_label} · ${p.required-p.open} of ${p.required} filled`,[fix(p,shift,kind),show(shift)]);
    if(compare)issue.since=compare.place(p.shift_id,Number(p.open));
    // A hand-off, not a dead end: who can change eligibility when it is not you (no extra authority is given here).
    if(!credentialed&&['eligibility','skill'].includes(kind))issue.reason+=' · a credentialer grants it';
    // People the whole run left out are said once above the cards (summary.excluded_people), not again on each card.
    if(issue.more&&leftOut.length)issue.more=issue.more.filter(m=>!leftOut.some(l=>m.endsWith(' '+l)));
    return issue;});
  // A problem People fixes (a skill, eligibility, a rank) gets that one fix, and the same hand-off when it is not yours.
  const PEOPLE_FIXES:Record<string,PlaceFix>={CAPABILITY_GAP:'skill',ELIGIBILITY_MISSING:'eligibility',NOT_ELIGIBLE:'eligibility',MEMBERSHIP_MISSING:'rank',RANK_NOT_ALLOWED:'rank'};
  // Blocking problems as few cards (hundreds of findings read as their causes): the server's cause groups of this
  // proposal's check (rota_summary.cause_groups), then the rest one card per rule (or kind), each person and session
  // with its numbers behind Who. Nothing is dropped: every finding is in exactly one card, and the card says how many.
  const one=(f:Row)=>{const shift=f.shift_id?byId.get(f.shift_id):undefined,kind=PEOPLE_FIXES[f.code]||'';
    const issue=findingCard(f,shift,[kind?fix(f,shift,kind):null,show(shift)]);
    if(!credentialed&&['eligibility','skill'].includes(kind))issue.reason='A credentialer grants it';
    return issue;};
  const where=(f:Row)=>{const x=f.shift_id?byId.get(f.shift_id):undefined;
    return x?[(x.service_ids as string[]).map(c=>name('services',c)).join(' + ')||x.type_snapshot?.name,dayName(x.start),hours(x.start,x.end)].filter(Boolean).join(' · '):f.start?formatDate(f.start,zone()):'';};
  const many=(list:Row[]):Issue=>{const f=list[0],kind=PEOPLE_FIXES[f.code]||'',rule=f.rule_id?find('policies',f.rule_id)?.name:undefined;
    const shown=list.map(x=>x.shift_id?byId.get(x.shift_id):undefined).find(Boolean);
    return {severity:'error',words:`${plural(list.length,'problem')}: ${rule||f.message}`,rule:rule&&rule!==f.message?{name:rule,mandatory:true}:undefined,
      reason:rule&&rule!==f.message?f.message:undefined,fold:true,whyTitle:`Who and where (${list.length})`,
      why:list.map(x=>({who:[x.person_id?name('people',x.person_id):'',where(x)].filter(Boolean).join(' · ')||'Session',reason:findingNumbers(x)||x.message})),
      actions:[kind?fix(f,shown,kind):null,show(shown)]};};
  const causeSets=data?mergeCauseGroups((s.rota_summary?.cause_groups||[]) as FindingCauseGroup[],s.rota_summary?.repair_tasks):[],byKey=new Map(mandatory.map(f=>[issueKey(f),f])),grouped=new Set<string>();
  const sets:{found:Row[];issue:Issue}[]=[];
  for(const g of causeSets){if(g.severity!=='error')continue;
    const found=g.finding_keys.map(k=>byKey.get(k)).filter(f=>f?.cause&&!grouped.has(issueKey(f))) as Row[];if(!found.length)continue;
    for(const f of found)grouped.add(issueKey(f));
    // In this panel the fix is never "run auto-schedule" (this is that run), and Show draws the proposal.
    sets.push({found,issue:causeGroupIssue(data!,{...g,fix_hints:g.fix_hints.filter(h=>h.action!=='run_auto_schedule')},found,close,show(found.map(f=>f.shift_id&&byId.get(f.shift_id)).find(Boolean)||undefined))});}
  const byRule=new Map<string,Row[]>();
  for(const f of mandatory)if(!grouped.has(issueKey(f))){const k=f.rule_id||f.code;byRule.set(k,[...(byRule.get(k)||[]),f]);}
  for(const list of byRule.values())sets.push({found:list,issue:list.length===1?one(list[0]):many(list)});
  for(const x of sets)if(compare)Object.assign(x.issue,sinceOfMany(x.found.map(compare.finding)));
  // What the proposal brings first (the decision is whether to apply it), then the biggest groups.
  const rank=(x:{found:Row[];issue:Issue})=>x.issue.since&&x.issue.since!=='draft'?0:1;
  const problems=sets.sort((a,b)=>rank(a)-rank(b)||b.found.length-a.found.length).map(x=>x.issue);
  const needs=openPlaces+mandatory.length;
  // Everything that stops submitting, counted once: open places and the other blocking problems of the whole rota.
  const what=[openPlaces?plural(openPlaces,'place'):'',mandatory.length?plural(mandatory.length,'problem'):''].filter(Boolean).join(' and ')||'Some places';
  const verb=agree(openPlaces+mandatory.length||1);
  // The server's outcome decides whether the run is complete (job.outcome); older stored jobs have none and are read from the counts.
  const complete=!needs&&(!job.outcome||job.outcome==='complete');
  // A group run says what it was asked (its job: who, and whether others' places were kept), never why it placed no one.
  const group=job.generation_scope?.conditions?`Only ${o.panel?conditionsSummary(job.generation_scope.conditions,namer(ctx()).label):conditionWords(job.generation_scope)} were scheduled${job.generation_scope.protection==='keep_existing'?', keeping existing assignments':''}. `:'';
  const status=complete&&!changes?outcome('ok',group?'No changes for this group':'Nothing to change',`${group}Every place is filled and every mandatory rule is met.`)
    :complete?outcome('ok',needed&&filled===needed?`All ${plural(needed,'place')} filled`:needed?`${filled} of ${needed} places filled`:'Ready to apply','',o.optimality)
    :changes?outcome('warn',`${what} ${verb} you before this rota can be submitted`,`Apply the ${plural(changes,'change')} now${needed?` (${filled} of ${needed} places filled)`:''}, then fix ${needs===1?'it':'these'} below.`)
    // No changes proposed: said as what this run did, never that nothing can be filled (the run does not prove that).
    :outcome('err',`${what} ${verb} you before this rota can be submitted`,`${group}This run proposed no changes. Fix these below, then run auto-schedule again.`);
  // The cards are counted beside the problems they hold when they group them.
  const head=[openPlaces?plural(openPlaces,'place'):'',mandatory.length?plural(mandatory.length,'problem')+(sets.length<mandatory.length?` in ${plural(sets.length,'group')}`:''):''].filter(Boolean).join(' · ');
  const draft=data?.workspace.status==='draft';
  // The basic (greedy) scheduler says what it did not weigh; this changes what the proposal means, so it stays visible.
  // In plain words first, then the scheduler's own list under "How this was made".
  // The server words each item for people, older runs too (serialization.py: by the item's input id).
  const notApplied=((r.not_applied||[]) as Row[]).map(x=>String(x.message));
  // A3/A4: the run in Fix-list terms (autoschedule_result.ts) once the result carries the Fix list; the clash and a
  // rota that changed during the run are said in either layout.
  const facts=runFacts(job),saved=s.score_saved??s.score,absence=facts.keptAbsence;
  const retained=data&&o.panel&&absence?runIssueCard(data,absence,byId.get(absence.shift_id),close,o.strict):undefined;
  const fixed=resultHead(facts,{changes,scoreDelta:saved?scoreDelta(s.score_before,saved):null,stale:drifted(job),optimality:o.optimality,strict:o.strict,nextIssue:retained?.words});
  const clash=isClash(facts)&&data?clashCard(facts.conflict!,clashHooks(data,close)):'';
  const changed=data?inputsChanged(job,facts,{person:id=>name('people',id),session:id=>{const x=byId.get(id);return x?dayName(x.start):'';}}):null;
  const drift=driftNote(job)||'';
  // AGENTS §2: the fact that changes what the proposal means stays in sight (one line after the numbers); the plain
  // explanation is one closed "How this was made", last, after the next step.
  const basicFact=notApplied.length?el('p',{class:'hint auto-basic-fact',role:'note'},'Basic scheduler · wishes and most advisory goals are not weighed'):'';
  const basicHow=notApplied.length?el('details',{class:'auto-how'},el('summary',{},'How this was made'),
    el('ul',{'aria-label':'How this was made'},el('li',{},'Made by the basic scheduler: each open place gets someone your mandatory rules allow.'),
      el('li',{},'Wishes and most advisory goals, such as fair shares and weekly spread, are not weighed.'),...notApplied.map(m=>el('li',{},m)))):'';
  // D76 (run card): one sentence, the four numbers, then the one next step; how it was made and what changes after.
  // Why the search stopped is the solver receipt's, which every caller shows above this (the run card, the result
  // dialog); a run continued past a rule conflict says its breaks are must-fixes.
  const breaks=breaksNote(r);
  // The outcome leads (A3); the breaks, like the basic scheduler's fact, are the line after the numbers.
  if(fixed)return el('section',{class:'auto-review','aria-label':'Review'},drift,...(changed?changed.nodes:[fixed.nodes[0]]),...fixed.nodes.slice(1),breaks,basicFact,clash,
    data?stillToFix(facts,fixHooks(data.workspace.id,data),()=>{close();openIssues(data.workspace.id);},retained):'',completionLine(job),
    o.panel?changesByCause(facts,changes)||changesSection(changeGroups(r,(data?.workspace.validation?.findings||[]) as Row[])):'',basicHow);
  // #1160: a stopped run's score is unfinished (the solver receipt says so): no Score of its own beside it.
  const reviewStatus=facts.fixListMissing
    ?outcome('warn',facts.fixListMissing,'Run auto-schedule again to get the detailed Fix list.')
    :status;
  return el('section',{class:'auto-review','aria-label':'Review'},drift,...(changed?.nodes||[reviewStatus]),breaks,clash,
    basicFact,resultMetrics(r.score_completed===false?{...s,score_saved:null,score:null}:s,changes,Number(r.validation?.error_count??findings.filter(f=>f.severity==='error').length),data?.workspace.validation?.score,o.strict!==false),
    o.panel?changesSection(changeGroups(r,(data?.workspace.validation?.findings||[]) as Row[])):'',
    needs?el('section',{class:'auto-needs'},el('h3',{},`Needs you · ${head}`),
      // Nothing proposed and both checks current: these are the draft's own problems, fixed in batches in Resolve issues.
      !changes&&compare&&draft&&data&&mandatory.length?el('p',{class:'hint'},'Fix them in the ',button('Fix list',()=>{close();openIssues(data.workspace.id);},'link-button small'),'.'):'',places.length>1&&rankedReasons(s).length>1?mainReasons(s):'',issueList([...cards,...problems],{label:'Needs you',done:close,interactive:false})/* a proposal's problems: each card's own fixes; no Open all into the draft's Fix list */,
      openCount>places.length?el('p',{class:'muted'},`${plural(openCount-places.length,'more session')} with open places ${openCount-places.length===1?'is':'are'} in Review latest result.`):''):'',
    // Advisory notes never stop Apply: a labelled list by rule; accepting them is saved in Resolve issues once applied.
    advisory.length?el('section',{class:'auto-advice','aria-label':'Advisory notes'},el('h3',{},`${plural(advisory.length,'advisory note')} · review later`),
      el('ul',{},...((s.advisory||[]) as Row[]).map(a=>el('li',{},`${a.name} · ${a.count}`))),
      draft&&data?el('p',{class:'hint'},'After applying, review or accept them in the ',button('Fix list',()=>{close();openIssues(data.workspace.id);},'link-button small'),'.'):''):'',
    s.wishes?.total?el('p',{class:'hint'},`Wishes: ${s.wishes.met} of ${s.wishes.total} met.${s.wishes.total>s.wishes.met?` The ${s.wishes.total-s.wishes.met} not met are listed per person in the People view.`:''}`):'',basicHow);
}
/** The clash card's choices (D1, D16, R4): relax the non-safety cap for this rota inline, ask a credentialer to make
 *  people eligible, open the rules. Nothing changes without the coordinator's press; a relax re-reads the rota. */
function clashHooks(data:WorkspaceData,close:()=>void){
  const go=(run:()=>unknown)=>()=>{close();return run();};
  return {ruleName:(id:string)=>String(find('policies',id)?.name||''),person:(id:string)=>name('people',id),
    openRule:(id:string)=>go(()=>openRuleSource(id).catch(reportError))(),
    eligibility:()=>go(()=>{openFiltered('people',{status:['Active']});navigate('people');})(),
    clinic:(id:string)=>name('services',id),
    // The existing exception flow, on the day of the cause's first session (or its first day).
    exception:(d:Row)=>{const person=String(d.person_id||(d.person_ids||[])[0]||''),first=data.shifts.find(x=>(d.session_ids||[]).includes(x.id));
      return d.rule_id&&person&&(first||d.starts_on)?exceptionButton(String(d.rule_id),person,first?.start||`${d.starts_on}T12:00:00Z`):null;},
    workspace:data.workspace,done:refresh};
}
/** Why a run stopped or did not start (findings.ts runError words): each rule line it can't schedule opens on Rules; with
 *  no list, Open Rules; an unknown code offers Send feedback with its reference and run. */
function runErrorNode(code:string,refusal:unknown,runId:string,close?:()=>void):HTMLElement{
  // A refused start passes the 422's details (the list); a failed run its stored error_detail {details, total} (same entries).
  const r=refusal as {details?:unknown;total?:number}|unknown[]|null|undefined,issues=Array.isArray(r)?r:r?.details,total=Array.isArray(r)?undefined:r?.total;
  const e=runError(code,issues,total),go=(hash:string)=>{close?.();navigate(hash);};
  const lines=e.lines.map(l=>{const policy=find('policies',l.rule_id),rule=policy?.definition?.rule,line=l.line==null?undefined:rule?.lines?.[l.line];
    const names=namer(ctx()),where=line?[countWords(line.who,names,''),countWords(line.where,names,'')].filter(Boolean).join(' · ')||'Everyone':'';
    const hash=rule?.kind==='catalogue'?routeHash('rules',line?.id?[rule.id,line.id]:[rule.id]):routeHash('rules',[],{source:l.rule_id});
    const a=el('a',{href:hash},[policy?labelOf(policy):'A rule',where].filter(Boolean).join(' · '));
    a.addEventListener('click',ev=>{ev.preventDefault();go(hash);});
    return el('li',{},a,l.reason?el('span',{class:'muted'},' '+l.reason):'');});
  return outcome('err',e.head,e.body,lines.length?el('ul',{class:'run-error-lines'},...lines):'',
    e.action==='rules'?el('div',{class:'actions'},button('Open Rules',()=>go(routeHash('rules')),'secondary small')):
    e.action==='feedback'?el('div',{class:'actions'},button('Send feedback',()=>{close?.();openFeedback({error:`Reference: ${code}${runId?` · Run ${runId}`:''}`});},'secondary small')):'');
}
async function inspectSchedulingJob(job:Row){const result=(await get('/api/jobs/'+job.id)).job;
  const rota=await rotaWhole(result.workspace_id).catch(()=>null);
  const currentDraft=!!rota&&rota.workspace.status==='draft'&&!rota.workspace.archived_at&&permits(CAPABILITY.COORDINATOR);
  const fits=rota?currency(result,rota.current_fingerprint):'unknown',editable=currentDraft&&currentGeneration(result,rota!);
  showDialog((result.kind==='validation'?TERMS.backgroundCheck:TERMS.autoSchedule)+(result.result?' results':' run details'),formatDate(result.created_at,zone())+' · '+formatTime(result.created_at,zone()),(body,close)=>{
  body.classList.add('auto-result');
  // Read-only only when the result no longer matches the rota (or cannot be checked): a current result says nothing here.
  const stale=!rota?'The rota could not be checked, so this result cannot be applied.'
    :rota.workspace.archived_at?'This rota is archived, so this result cannot be applied.':rota.workspace.status!=='draft'?'This rota is no longer a draft, so this result cannot be applied.':STALE_WORDS[fits];
  if(result.kind==='generation'&&result.result&&!editable&&stale)body.append(note(stale),
    fits==='superseded'&&rota?el('div',{class:'actions'},button(SUPERSEDED.next,()=>{close();reviewLatestRun(rota.workspace.id);},'secondary small')):'');
  // Support detail (timing, evidence, the whole rota check) is one disclosure at the end: the result comes first.
  const details=el('details',{class:'auto-run-details'},el('summary',{},'Run details'),el('p',{class:'run-metrics'},jobTiming(result)));
  const r:Row|null=result.result;
  const outsideGroup=outsideGroupLine(result);
  if(outsideGroup)body.append(outsideGroup);
  if(r?.phase_timings){
    const stages=generationPhaseTimings(r.phase_timings,runSeconds);
    if(r.first_solution_seconds!=null)stages.push(`first candidate after ${runSeconds(Number(r.first_solution_seconds))}`);
    details.append(el('p',{class:'muted run-phases'},stages.join(' · ')));
  }
  // Why the fair share search stopped (its warning, when there is one, is with the other warnings above the details).
  const fair=r?.fair_share_search as Row|undefined;
  if(fair?.stop_reason)details.append(el('p',{class:'muted run-fairness'},'Fair share search: '+(FAIR_STOP_WORDS[fair.stop_reason]||String(fair.stop_reason).replaceAll('_',' '))));
  if(result.error_code)body.append(runErrorNode(String(result.error_code),result.error_detail,String(result.id),close));
  if(!r){if(!result.error_code)body.append(outcome('neutral',result.status==='cancelled'?'This run was cancelled':'No completed result is available',result.status==='cancelled'?'No completed result is available.':''));body.append(details);return;}
  const receipt=solverReceipt(result);
  if(receipt)body.append(receipt);
  if(result.kind==='validation'){body.append(findingsBlock(r as unknown as Evaluation));return;}
  const reviewable=!!r.validation&&['candidate_feasible','candidate_with_gaps','unknown_time_limit'].includes(r.status);
  if(r.status==='requirements_conflict'){const facts=runFacts(result);body.append(shortfallBlock(r,'',Infinity),isClash(facts)&&rota?clashCard(facts.conflict!,clashHooks(rota,close)):'');}
  else if(r.status==='needs_more_time')body.append(outcome('neutral','More search time needed',GENERATION_RESUME_NOTE));
  else if(reviewable)body.append(proposalSummary(stale?null:rota,result,{editable,strict:!!rota&&currentGeneration(result,rota)&&strictlyCurrent(result,rota.current_fingerprint),optimality:candidateOptimality(result,!!rota&&currentGeneration(result,rota)&&strictlyCurrent(result,rota.current_fingerprint)),panel:false,close}));
  else body.append(outcome(r.status==='unknown_time_limit'?'neutral':'err',r.status==='unknown_time_limit'?'No valid proposal was found within the search time':'No proposal to apply',
    r.status==='unknown_time_limit'?'The rota is unchanged; the result does not establish that its rules conflict.':`The rota is unchanged. ${nothingWhy(r)}`));
  if(reviewable&&r.status==='unknown_time_limit')body.append(note('The search time ran out: review this proposal before applying it.','warning'));
  // SW3-A-08: one line per shortfall, never one "a.; b.; c." string; live check F3: the one issue list, so the same
  // sentence (one per search phase) is said once.
  if(r.warnings?.length)body.append(issueList((r.warnings as string[]).map(x=>({severity:'warning',words:String(x)})),{label:'Run warnings',interactive:false}));
  // Places nobody can fill: one table, grouped by duty, each row with who could have worked it and why not.
  const shortages:Row[]=(r.diagnostics||[]).map((d:Row,i:number)=>({...d,_key:String(i)}));
  if(shortages.length){
    const where=reviewable?details:body;
    const duty=(d:Row)=>[d.requirement_name,...(d.service_ids||[]).map((id:string)=>name('services',id))].filter(Boolean).join(' · ')||'Scheduling requirement';
    where.append(el('h3',{},'Places nobody can fill'),dataTable<Row>({id:'shortages',noun:['place','places'],rows:shortages,rowId:d=>d._key,pageSize:25,defaults:{group:'duty',hidden:['duty']},
      searchLabel:'Search places',searchPlaceholder:'Duty or clinic',
      columns:[
        {key:'duty',label:'Duty',text:duty,facet:duty,cell:d=>el('strong',{},duty(d))},
        {key:'day',label:'Day',sort:d=>String(d.start||''),facet:d=>d.start?formatDate(d.start,zone()):'—',cell:d=>d.start?dateUnits([formatDate(d.start,zone())]):'—'},
        {key:'time',label:'Time',cell:d=>d.start?hours(d.start,d.end):'—'},
        {key:'needs',label:'Needs',num:true,sort:d=>Number(d.required||0),cell:d=>String(d.required??'—')},
        {key:'can',label:'Can work',num:true,sort:d=>Number(d.available_upper_bound??0),facet:d=>Number(d.available_upper_bound)===0?'Nobody':'Some',cell:d=>Number(d.available_upper_bound)===0?badge('Nobody','err'):String(d.available_upper_bound??'—')},
      ],
      rowButtons:d=>d.candidate_count?[button('Who and why',()=>candidateReview(result.id,name,zone(),Number(d._key),undefined,d),'small')]:[]}).root);
  }
  const capacityIssues=new Map<string,Row[]>(),others:Row[]=[];
  for(const issue of r.issues||[]){
    if(issue.code==='RESOURCE_CAPACITY'&&issue.detail?.resource_id){
      const key=issue.detail.resource_id;
      capacityIssues.set(key,[...(capacityIssues.get(key)||[]),issue]);
    }else others.push(issue);
  }
  // Stored findings keep their recorded context; the current Resolve page checks fresh inputs before editing.
  // Live check F3: the one issue list (its D78 mark, each sentence once with its dates as chips), ending "Open all in
  // Fix list (N)" on the current draft.
  if(others.length){body.append(el('h3',{},'Resolve these first'),issueList(others.map(issue=>({...findingItem(issue,issue.start?spanLabel(issue.start,issue.end,zone()):''),severity:'error'})),
    {label:'Resolve these first',done:close,...(currentDraft?{openAll:{count:others.length,go:()=>openIssues(rota!.workspace.id)}}:{})}));}
  if([...capacityIssues.values()].some(rows=>rows.some(issue=>issue.detail.published_sessions)))
    body.append(note('This draft overlaps a published rota. If it replaces that rota, start a reviewed amendment from Published rota; otherwise choose another period.','error'));
  for(const [resourceId,rows] of capacityIssues){
    const intervals=[...new Map(rows.map(issue=>[`${issue.start}|${issue.end}`,issue])).values()];
    const worst=Math.max(...intervals.map(issue=>Number(issue.detail.scheduled_sessions)));
    const capacity=intervals[0].detail.capacity;
    const title=`${name('locations',resourceId)} · ${worst} concurrent sessions, capacity ${capacity} · ${intervals.length} affected interval${intervals.length===1?'':'s'}`;
    body.append(el('section',{class:'auto-capacity','aria-label':title},el('h3',{},title),
      table(['From','Until','Sessions','Published'],intervals.map(issue=>[stamp(issue.start),stamp(issue.end),String(issue.detail.scheduled_sessions),String(issue.detail.published_sessions||0)]))));
  }
  // Every change, to review and choose: each starts ticked; untick any (one by one, or by day or person through the
  // table's filters and "select all shown") and apply only the chosen ones.
  const additions:Row[]=r.additions||[],removals:Row[]=r.removals||[],total=additions.length+removals.length;
  if(total){
    const pickAdd=new Set<string>(additions.map((_:Row,i:number)=>String(i))),pickRemove=new Set<string>(removals.map((_:Row,i:number)=>'r'+i));
    const actions=el('div',{class:'actions'});
    const applyJob=async(additionIndexes?:number[],removalIndexes?:number[])=>{
      const reply=await sendGenerationApply(result,additionIndexes?{addition_indexes:additionIndexes,removal_indexes:removalIndexes||[]}:undefined,()=>{
        selectionHint.hidden=true;proposal=null;
        actions.replaceWith(outcome('warn',SUPERSEDED.state,STALE_WORDS.superseded,button(SUPERSEDED.next,()=>{close();reviewLatestRun(result.workspace_id);},'secondary small')));});
      // Nothing written: the dialog stays, saying so where Apply was (nothing is left to apply).
      if(nothingApplied(reply)){selectionHint.hidden=true;actions.replaceWith(outcome('err',...NOTHING_APPLIED));proposal=null;await refresh();return;}
      if(applyNeedsReview(reply)){selectionHint.hidden=true;actions.replaceWith(outcome('warn',...APPLY_NOT_CONFIRMED));proposal=null;await refresh();return;}
      close();proposal=null;await refresh();};
    const applyChosen=button('Apply chosen',()=>applyJob([...pickAdd].map(Number),[...pickRemove].map(k=>Number(k.slice(1)))),'secondary');
    const selectionHint=el('p',{class:'hint',role:'status'});
    const syncChosen=()=>{const n=pickAdd.size+pickRemove.size;
      const incomplete=[...pickAdd].some(k=>{const a=additions[Number(k)];return removals.some((x:Row,i:number)=>x.shift_id===a.shift_id&&x.person_id===a.participant.person_id&&!pickRemove.has('r'+i));});
      applyChosen.textContent=`Apply chosen (${n} of ${total})`;applyChosen.disabled=!n||n===total||incomplete;
      selectionHint.textContent=incomplete?'Select the matching replacement removal too, or deselect its new assignment.':'';selectionHint.hidden=!incomplete;};
    const dayOf=(iso:string)=>formatDate(iso,zone()),draftFindings=(rota?.workspace.validation?.findings||[]) as Row[];
    body.append(el('h3',{},'All changes'));
    // The day has its own column: the time column says only the hours, so the table fits the dialog.
    if(removals.length)body.append(dataTable<Row>({id:'schedule-removals',noun:['person taken off','people taken off'],rows:removals.map((x:Row,i:number)=>({...x,_key:'r'+i})),rowId:x=>x._key,pageSize:25,
      ...(editable?{selection:{set:pickRemove,changed:syncChosen}}:{}),
      columns:[
        {key:'person',label:'Taken off',text:x=>name('people',x.person_id),facet:x=>name('people',x.person_id),cell:x=>el('strong',{},name('people',x.person_id))},
        {key:'duty',label:'Duty',text:x=>x.session_label||'',facet:x=>x.session_label||'Not recorded',cell:x=>x.session_label||'Not recorded'},
        {key:'day',label:'Day',sort:x=>String(x.start),facet:x=>dayOf(x.start),cell:x=>dayOf(x.start)},
        {key:'time',label:'Time',sort:x=>String(x.start),cell:x=>hours(x.start,x.end)},
        {key:'reason',label:'Why',text:x=>removalWhy(x,draftFindings),cell:x=>removalWhy(x,draftFindings)},
      ]}).root);
    if(additions.length)body.append(dataTable<Row>({id:'schedule-additions',noun:['new placement','new placements'],rows:additions.map((a:Row,i:number)=>({...a,_key:String(i)})),rowId:a=>a._key,pageSize:25,defaults:{sort:['time','asc']},  // day and time
      searchLabel:'Search changes',searchPlaceholder:'Person, duty or clinic',
      ...(editable?{selection:{set:pickAdd,changed:syncChosen}}:{}),
      columns:[
        {key:'day',label:'Day',sort:a=>String(a.participant.start),facet:a=>dayOf(a.participant.start),cell:a=>dayOf(a.participant.start)},
        {key:'person',label:'Person',text:a=>name('people',a.participant.person_id),facet:a=>name('people',a.participant.person_id),cell:a=>el('strong',{},name('people',a.participant.person_id))},
        {key:'duty',label:'Duty',text:a=>a.session_label||'',facet:a=>a.session_label||'Not recorded',cell:a=>a.session_label||'Not recorded'},
        {key:'clinic',label:'Clinic',text:a=>(a.service_names||[]).join(', '),facet:a=>a.service_names?.length?a.service_names:['None'],cell:a=>(a.service_names||[]).join(', ')||'—'},
        {key:'location',label:'Location',text:a=>a.location_name||'',facet:a=>a.location_name||'Not recorded',cell:a=>a.location_name||'Not recorded'},
        {key:'time',label:'Time',sort:a=>String(a.participant.start),cell:a=>hours(a.participant.start,a.participant.end)},
        {key:'participation',label:'Role',facet:a=>PARTICIPATION[a.participant.participation]||String(a.participant.participation),cell:a=>PARTICIPATION[a.participant.participation]||a.participant.participation},
        {key:'overtime',label:'Overtime',facet:a=>a.participant.overtime?'Yes':'No',cell:a=>a.participant.overtime?'Yes':'No'},
      ]}).root);
    if(editable){syncChosen();actions.append(button(rechecked(result)?'Check again and apply':total===1?'Apply the change':`Apply all ${total} changes`,()=>applyJob(),'primary'),applyChosen,el('span',{class:'hint'},'You can undo'));body.append(selectionHint,actions);}
  }
  if(r.optimality_boundary)details.append(el('p',{},r.optimality_boundary),
    ...(r.objective_approximations||[]).map((a:Row)=>el('p',{},`${name('policies',a.rule_id)}: ${a.searched_opportunities} of ${a.opportunities} pairing opportunities searched.`)));
  if(r.redistribution?.attempted)details.append(el('p',{},r.redistribution.improved?'Reconsidering new suggestions reduced the remaining gaps.':'Reconsidering new suggestions did not reduce the remaining gaps.'));
  if(r.candidate_evidence_count)details.append(el('div',{class:'actions'},button('View candidates considered',()=>candidateReview(result.id,name,zone()),'secondary small')));
  if(r.validation)details.append(findingsBlock(r.validation));
  body.append(details);
} );}

// Proposals the coordinator discarded in this browser session (the job itself stays in history).
const dismissedJobs=new Set<string>();
// The rota's revision is the one this page last read or changed (a reply carries it); it reads it only when it holds none.
async function sendGenerationApply(job:Row,pick?:{addition_indexes:number[];removal_indexes:number[]},here?:()=>void){
  const revision=knownRevision(job.workspace_id)??(await rotaMeta(job.workspace_id)).workspace.revision;
  return command('generation.apply',{workspace_id:job.workspace_id,job_id:job.id,reason:'Applied auto-schedule suggestions',...pick},undefined,revision).catch(e=>appliedAnyway(job.id,e)).catch(e=>supersededRun(e,job.workspace_id,here));
}
const applyJob=async(job:Row,pick?:{addition_indexes:number[];removal_indexes:number[]})=>{
  const reply=await sendGenerationApply(job,pick);proposal=null;
  if(nothingApplied(reply)){await refresh();throw new Error(NOTHING_APPLIED.join('. '));}
  if(applyNeedsReview(reply)){await refresh();throw new Error(APPLY_NOT_CONFIRMED.join('. '));}};
/** SW3-A-04: an apply whose answer was lost (a gateway timeout under load, a dropped connection) may have been made all
 *  the same: the run says so. Applied: the page is told it was saved (APPLY_CONFIRMED) and nothing offers Apply again; a
 *  refusal (4xx) or a run not applied throws the error as it was. */
async function appliedAnyway(jobId:string,e:unknown):Promise<typeof APPLY_CONFIRMED>{
  if(!(e instanceof ApiError)||e.status>0&&e.status<500)throw e;
  const job=await get<{job:Row}>('/api/jobs/'+jobId).then(r=>r.job,()=>null);
  if(job?.status!=='applied')throw e;
  window.dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'generation.apply',result:APPLY_CONFIRMED}}));return APPLY_CONFIRMED;
}
let lastJobs:{id:string;jobs:Row;activity?:number|null;woke:number}|null=null;
// A jobs read started before Apply may still say the run is ready for review.
let generationApplyEpoch=0;
/** The rota and revision last read again because a run was applied after the draft on screen (SW3-A-05): once each. */
let behindRead='';
/** The rota's latest auto-schedule run left a proposal that still fits it (not applied, discarded or out of date). */
function proposalWaiting(data:WorkspaceData):boolean{return !!waitingJob(data);}
/** SX-25: a finished run that proposes no change (nothing added, nothing removed) is not a proposal to review. */
const nothingToApply=(j:Row)=>{const r=j.result;return !!r&&(r.change_count===0||Array.isArray(r.additions)&&Array.isArray(r.removals)&&!r.additions.length&&!r.removals.length);};
/** H-01: why a finished run has nothing to apply, and what to do next, in plain words (Recent runs, the panel, the run's
 *  details). From the run's result status; the rota is unchanged in every case. */
function nothingWhy(r:Row|null|undefined):string{
  const status=String(r?.status||''),valid=r?.validation;
  if(['translation_error','implementation_error','model_invalid'].includes(status))return `The run's result failed the final rule check${valid?.error_count?` (${mustWords(valid.error_count)})`:''}, so it was not offered. Run auto-schedule again; if it happens again, open the run details and send feedback.`;
  if(status==='requirements_conflict')return 'Some places can’t be filled by anyone, so nothing else was changed. Review the run to see who and why.';
  if(status==='unknown_time_limit'||status==='needs_more_time')return 'The search time ran out before a better rota was found. Run again with more search time.';
  if(status==='invalid_inputs')return 'The draft has conflicts the run must keep. Fix them from the Fix list, then run again.';
  if(status.startsWith('unsupported_')||status==='configuration_needs_attention')return 'The run’s setup needs attention. Open the run details to see what to change.';
  if(status==='candidate_feasible'||status==='candidate_with_gaps')return 'No change would improve this rota under the current rules and priorities.';
  return String(r?.message||'The run found no change to make.');
}
/** That run's job (from the rota's job list), or undefined: current only when the server says so (`currency`). */
function waitingJob(data:WorkspaceData):Row|undefined{
  const j=lastJobs?.id===data.workspace.id?((lastJobs.jobs.jobs||[]) as Row[]).find(x=>x.kind==='generation'):undefined;
  return j&&j.status==='succeeded'&&!nothingToApply(j)&&!currency(j,data.current_fingerprint)&&!dismissedJobs.has(j.id)&&['complete','partial'].includes(j.outcome)?j:undefined;
}
/** The way to the rota's latest run: Auto-schedule open on its Review step (the latest run is the one it shows). */
function reviewLatestRun(id:string){
  // SW-07: the proposal's review opens wherever it was asked from: a session panel in the one slot is closed (it would
  // cover the result; unsaved edits ask first), and the rota's own Auto-schedule state is the one opened on its result.
  if(!dockLeave(()=>reviewLatestRun(id)))return;
  const s=schedulingByRota.get(id)||{...freshScheduling(id,true),...ownSetup(id)};
  Object.assign(s,{open:true,step:3});schedulingByRota.set(id,s);schedulingSettings=s;
  if(schedView==='year')switchView('week');
  if(view==='schedule'&&workspaceId===id&&!schedSubPage()){void renderPage();return;}
  workspaceId=id;draftNone=false;navigate(routeHash('schedule',[schedView,planDay],{edit:id}).slice(1));
}
/** Opens the waiting proposal's review (Auto-schedule, its result step) on this rota's schedule: the one way to it. */
const reviewProposal=(data:WorkspaceData)=>reviewLatestRun(data.workspace.id);
/** The waiting proposal said in one place on the Schedule: one footer pill that opens its review (the Auto-schedule
 *  button carries the same mark). Gone once it is applied, discarded or out of date. */
function proposalPill(data:WorkspaceData){
  const b=el('button',{type:'button',class:'health-pill info proposal-pill','aria-label':'Auto-schedule proposal ready · review it','data-ctip':'Proposal ready · review it'},icon('sparkle'),el('span',{class:'label'},' Proposal ready'));
  footerAct(b,'proposal',()=>reviewProposal(data));return b;
}
const proposalShown=(data:WorkspaceData)=>data.workspace.status==='draft'&&!data.workspace.archived_at&&permits(CAPABILITY.COORDINATOR)&&proposalWaiting(data);
/** After the jobs are read: the button's mark and the footer pill follow them (the bar was drawn before the read). */
function markProposal(data:WorkspaceData){
  const waiting=proposalShown(data),b=document.querySelector<HTMLButtonElement>('.auto-button'),pills=document.querySelector('.app-footer .health-pills');
  if(b){b.classList.toggle('has-proposal',waiting);b.setAttribute('aria-label',waiting?'Auto-schedule · proposal ready':'Auto-schedule');}
  pills?.querySelector('.proposal-pill')?.remove();
  if(waiting&&pills)pills.prepend(proposalPill(data));
}
/** The rota's job list when it is not held yet (Resolve issues or Submit opened first): one read, failures say nothing. */
async function readJobs(data:WorkspaceData){
  if(!permits(CAPABILITY.COORDINATOR)||data.workspace.status!=='draft'||lastJobs?.id===data.workspace.id&&lastJobs.activity===seenActivity)return;
  const woke=panelWakes,r=await get<Row>('/api/jobs?workspace_id='+encodeURIComponent(data.workspace.id)).catch(()=>null);
  if(r)lastJobs={id:data.workspace.id,jobs:r,activity:seenActivity,woke};
}
/** Resolve issues and Submit: the waiting proposal said once above the issues, with its review. */
const proposalNote=(data:WorkspaceData,done:()=>void=()=>{})=>proposalShown(data)?el('div',{class:'span-2 issues-cta proposal-note'},
  note('An auto-schedule proposal is waiting for this rota. Review it before fixing these one by one: editing the draft makes it out of date.','info'),
  button('Review the proposal',()=>{done();reviewProposal(data);},'secondary small')):null;
// Proposals the coordinator chose to let go out of date by editing the draft: asked once each.
const letStale=new Set<string>();
/** Before a draft edit that would make the waiting proposal out of date: say so once, with its review as the way back.
 *  The run stays in Auto-schedule history; it only can no longer be applied (its inputs changed). */
guardDraftEdits(async(kind,payload,target)=>{
  const items=kind==='bulk.apply'?((payload.items||[]) as Row[]).map(i=>i.payload?.workspace_id).filter(Boolean):[];
  // A session edit names its session, not its rota: it is the open draft's only when made on the Schedule.
  const id=String(payload.workspace_id||items[0]||(kind.startsWith('workspace.')?target:'')||(kind!=='bulk.apply'&&view==='schedule'?workspaceId:'')||''),data=scheduleCache.drafts.get(id);
  const job=data&&proposalShown(data)?waitingJob(data):undefined;
  if(!data||!job||letStale.has(job.id))return true;
  return new Promise<boolean>(resolve=>{let said=false;const answer=(yes:boolean)=>{if(!said){said=true;resolve(yes);}};
    // SW-07: an edit after the run does not throw it away (maintenance reconciles it: "Check again and apply"), as an edit
    // during the run does; the question only lets the coordinator look at the proposal first.
    showDialog('Edit the draft now?','The waiting auto-schedule proposal was made from the draft as it is now. After this edit it is checked again before you apply it: what still fits is kept.',(body,close,_t,onClose)=>{
      onClose(()=>answer(false));
      body.append(el('div',{class:'actions'},button('Review the proposal',()=>{answer(false);close();reviewProposal(data);},'secondary'),
        button('Edit anyway',()=>{letStale.add(job.id);answer(true);close();},'primary')));});});
});
let lastSchedulingBody:{id:string;feedback:Node[];history:Node[]}|null=null;
// Auto-schedule live view: the module loads only when a run is shown.
let liveModule:Promise<typeof import('./autoschedule_live.js')>|null=null;
const liveView=()=>liveModule??=import('./autoschedule_live.js');
onSessionReset(()=>{seenGeneration=undefined;void liveModule?.then(m=>m.endLive());});
// A control in a region that is drawn again (the run panel, every read): found again by a key that survives the redraw
// (data-action, href, or its name), then its place among controls with that key.
const FOCUSABLE='a[href],button,input,select,textarea,summary,[tabindex]';
const focusKey=(e:Element)=>`${e.tagName}|${e.getAttribute('data-action')||e.getAttribute('href')||e.getAttribute('aria-label')||e.textContent?.trim()||''}`;
const sameKey=(root:HTMLElement,key:string)=>[...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(e=>focusKey(e)===key);
function focusSpot(root:HTMLElement,e:Element){const key=focusKey(e);return {key,nth:sameKey(root,key).indexOf(e as HTMLElement)};}
/** Focus back on that control; gone or disabled: the progress line, else the result's status, else its first button. */
function refocusSpot(root:HTMLElement,{key,nth}:{key:string;nth:number}){
  const again=sameKey(root,key)[nth];
  const n=again&&!again.matches(':disabled')?again:root.querySelector<HTMLElement>('.auto-stage')??root.querySelector<HTMLElement>('.auto-outcome,[role=status],h2,h3')
    ??root.querySelector<HTMLElement>('button:not(:disabled)');
  if(!n)return;
  if(!n.matches(FOCUSABLE))n.tabIndex=-1;
  focusQuietly(n);
}
/** The footer's run strip from the moment Run is pressed, until the live view puts the run's own strip there. */
function startingStrip(){
  const status=document.querySelector<HTMLElement>('.app-footer .page-status>.footer-status');
  if(!status||phoneQuery.matches||status.querySelector('.asl-strip'))return null;
  const said=el('span',{class:'sr-only',role:'status'}),text=`1/5 ${LIVE_STEPS[0]}`;
  const n=el('span',{class:'asl-strip',role:'group','aria-label':'Auto-schedule run'},el('span',{class:'asl-stepbox','aria-hidden':'true'},el('span',{class:'asl-step'},text)),said);
  status.prepend(n);window.setTimeout(()=>{said.textContent=`Step 1 of 5: ${LIVE_STEPS[0]}`;},100);  // a live region speaks a change, not its first text
  return n;
}
/** What the live view reads from this rota: names, duty codes and hues, days in the department's zone. */
function liveHooks(data:WorkspaceData){
  const people=new Map(rows('people').map(p=>[p.id,p]));
  return {data,settings:()=>animationSettings(me!.department),day:(s:Row)=>toLocal(s.start,zone()).slice(0,10),
    duty:(s:Row)=>({code:itemHead(itemParts(ctx(),s,'',zone(),propertiesOf(weekView.props,'chip'),{only:ITEM_DRAWS.chip}))?.text??sessionCode(ctx(),s),hue:dutyColour(s),name:String(s.type_snapshot.name),time:`${formatTime(s.start,zone())}–${formatTime(s.end,zone())}`}),
    name:(id:string)=>displayName(people.get(id)),label:(id:string)=>fullNameOf(people.get(id)),redraw:()=>void renderPage(),
    read:async(id:string)=>(await get('/api/jobs/'+id+'/status?events=true')).job as Row};
}
/** A count or duration with its words never split across lines ("took < 1 s", "630 changes ready"). */
const unbroken=(text:string)=>text.replaceAll(' ',' ');
let panelVisibility:(()=>void)|null=null;
/** Returns to the tab, counted whichever panel (if any) owned the listener then. Each job-list read carries the count
 *  from when it started (`woke`): one that started before a return may predate what changed, so no panel draws it. A read
 *  started after a return never joins one started before it (readAfresh). */
let panelWakes=0;document.addEventListener('visibilitychange',()=>{if(!document.hidden){panelWakes++;readAfresh();}});
/** The open rota's job list, read alongside its sessions by the Schedule (renderSchedule) for the panel's first draw. */
let jobsEarly:{id:string;read:Promise<Row|null>;woke:number}|null=null;
const panelPulse=ownedListener('jobs-changed');
let panelJob='',releasePanelJob:(()=>void)|null=null;
function watchPanelJob(id:string){if(id===panelJob)return;releasePanelJob?.();panelJob=id;releasePanelJob=id?watchJob(id):null;}
/** Re-says whether Run can start when the schedule's selection changes. */
let autoRunSync:(()=>void)|null=null;
async function schedulingPanel(target:HTMLElement,data:WorkspaceData,editable:boolean,reuse:boolean,closePanel?:()=>void){
  const w=data.workspace,serial=renderSerial;
  if(schedulingSettings.workspace!==w.id){const open=schedulingSettings.open;
    schedulingSettings=schedulingByRota.get(w.id)||{...freshScheduling(w.id,open),...ownSetup(w.id)};schedulingSettings.open=open;schedulingByRota.set(w.id,schedulingSettings);}
  // A queued command may finish after another rota opens. Its continuation belongs to this rota's saved setup.
  const settings=schedulingSettings,st=()=>settings;
  // A view change reuses the jobs just read, unless something moved since (a run someone else started): nothing is
  // polled while no run is going (PL-03), so the department's activity says when to read again.
  // The panel's place shows its shape while the jobs are read, never an empty column (FD-03).
  const sk=skeleton('panel','Loading auto-schedule…');target.append(sk);
  // The page swaps in with this skeleton; an open People to schedule builder waits for its new button.
  const peopleKey=peoplePopoverKey(w.id),handover=awaitAdoption(peopleKey);
  try{
  const prefetched=jobsEarly?.id===w.id?jobsEarly:null;jobsEarly=null;
  const reused=reuse&&lastJobs?.id===w.id&&lastJobs.activity===seenActivity?lastJobs:null;
  let firstWoke=reused?reused.woke:prefetched?prefetched.woke:panelWakes;
  let initialJobs:Row|null=reused?reused.jobs:prefetched&&await prefetched.read;
  if(!initialJobs){firstWoke=panelWakes;initialJobs=await get<Row>('/api/jobs?workspace_id='+encodeURIComponent(w.id)).catch(e=>{sk.remove();throw e;});}
  if(serial!==renderSerial)return;
  const capability=initialJobs.generation_capability||{},cpSat=capability.engine==='cp_sat',maxDays=Number(capability.max_period_days)||31;
  // An unissued rota runs with the settings captured with it; the rota's job list says them (else the department's).
  const defaults={...capability.default_options,...('generation_defaults' in initialJobs?initialJobs.generation_defaults:me!.department.generation_defaults)} as GenerationOptions;
  const maxMinutes=Number(defaults.max_search_minutes)||AUTO_SCHEDULE_LIMITS.default_max_minutes;
  // A kept setup that no longer fits (a lower ceiling, other rota dates or clinics) falls back to the defaults.
  if(st().minutes!==undefined&&!(st().minutes!>=AUTO_SCHEDULE_LIMITS.minimum_minutes&&st().minutes!<=maxMinutes))st().minutes=undefined;
  if(st().from&&(st().from<w.starts_on||st().to>lastIncludedDate(w.ends_on)))Object.assign(st(),{from:'',to:''});
  const minutes=timeField('search_minutes',st().minutes??defaults.search_minutes,{max:maxMinutes});
  minutes.input.addEventListener('input',()=>{const n=minutes.value();if(Number.isFinite(n)){st().minutes=n;keptSetup(st());}});
  // Keep improving fairness for this run (the same field as Configuration › Timing; the department's value by default).
  if(st().fairness!==undefined&&!(st().fairness!>=0&&st().fairness!<=AUTO_SCHEDULE_LIMITS.maximum_stall_seconds))st().fairness=undefined;
  const fairness=fairnessPlateauField(st().fairness??defaults.fairness_plateau_seconds);
  fairness.input.addEventListener('input',()=>{const n=fairness.value();if(Number.isFinite(n)){st().fairness=n;keptSetup(st());}});
  // Stop when (Setup › Change): the department's stop conditions until changed for this run, kept for this rota while
  // the page is open (never on the device: Stop by is a time today). Stop by is refused once it has passed.
  const stops=stopFields({...defaults,...st().stops},()=>toLocal(new Date().toISOString().slice(0,16)+'Z',zone()).slice(11,16));
  const stopsChanged=()=>!stopsDefault(stops.read(),defaults);
  // A run chooses its search time, weight preset and whether existing assignments stay; other tuning is in Configuration.
  st().preset=knownPreset(st().preset);
  // Both schedulers start from the department setting reviewed.
  const keepByDefault=(defaults.mode??'fill_gaps')==='fill_gaps';
  // People to schedule: offered where the server accepts a group (its fields and protections). A kept group the server
  // no longer accepts is never sent, nor turned into everyone: it waits to be chosen again. A group run keeps every
  // existing assignment unless the server can rebalance the group alone and it is unticked, whatever the department's
  // default mode is.
  const scopeCap=scopeCapability(capability),rebalanceGroup=!!scopeCap?.protections.includes('rebalance_selected');
  if(st().group&&!(scopeCap&&parseGroup(st().group,scopeCap.fields)))Object.assign(st(),{group:null,pickGroup:true});
  const grouped=()=>!!st().group||st().pickGroup;
  const keeping=()=>grouped()?!rebalanceGroup||(st().groupKeep??scopeCap?.default_protection!=='rebalance_selected'):st().keep??keepByDefault;
  // Raw score weights and wish budgets are never run overrides (the server refuses differing ones); a preset is named.
  const runOptions=():GenerationOptions=>{const o={...presetOptions(defaults,st().preset),...stops.read(),search_minutes:minutes.value(),fairness_plateau_seconds:fairness.value(),mode:keeping()?'fill_gaps':'rebalance'} as GenerationOptions;
    if(o.scoring){const {weights:_w,wish_budget_points:_p,wish_period:_r,...rest}=o.scoring as Row;o.scoring=rest as GenerationOptions['scoring'];}return o;};
  const overridden=()=>st().preset!==DEFAULT_PRESET||keeping()!==keepByDefault||minutes.value()!==defaults.search_minutes||fairness.value()!==defaults.fairness_plateau_seconds||stopsChanged();

  // 1 · What to fill: dates (a month at most, the solver's limit), clinics, which sessions, and whether manual edits stay.
  const day=(s:Row)=>toLocal(s.start,zone()).slice(0,10),first=w.starts_on,last=lastIncludedDate(w.ends_on);
  const live=data.shifts.filter(s=>!s.cancelled);
  const openIds=new Set((w.validation?.coverage||[]).filter((c:Row)=>c.status==='gap').map((c:Row)=>c.shift_id));
  if(!st().from){
    // A long rota starts on its first month with an open place.
    const start=(Date.parse(last)-Date.parse(first))/864e5+1>maxDays?(live.filter(s=>openIds.has(s.id)).map(day).sort()[0]||first).slice(0,8)+'01':first;
    const from=start<first?first:start,end=addDays(from,maxDays-1);
    Object.assign(st(),{from,to:start===first&&end>=last?last:new Date(Date.parse(from+'T12:00:00Z')).getUTCMonth()===new Date(Date.parse(end+'T12:00:00Z')).getUTCMonth()?end:addDays(from.slice(0,8)+'01',0)});
    if(st().to===from.slice(0,8)+'01'||st().to>last){const m=new Date(from+'T12:00:00Z');m.setUTCMonth(m.getUTCMonth()+1,0);st().to=[m.toISOString().slice(0,10),last].sort()[0];}
  }
  const from=field('From','date',st().from,{required:true,min:first,max:last}),to=field('To','date',st().to,{required:true,min:first,max:last});
  from.input.addEventListener('change',()=>{st().from=from.input.value;keptSetup(st());syncRun();});to.input.addEventListener('change',()=>{st().to=to.input.value;keptSetup(st());syncRun();});
  const services=[...new Set(live.flatMap(s=>s.service_ids as string[]))].map(id=>({value:id,label:name('services',id),tone:'place'})).sort(byLabel);
  const clinics=el('div',{class:'field'},el('label',{'aria-hidden':'true'},'Clinics'),multiPick('Clinics',services,st().clinics=st().clinics.filter(c=>services.some(x=>x.value===c)),v=>{st().clinics=v;keptSetup(st());syncRun();},'All clinics').wrap);
  const scope=select('Assign into',[{value:'period',label:'All sessions in these dates'},{value:'open',label:'Open sessions only'},{value:'selected',label:'Selected sessions only'}],st().scope);
  scope.input.addEventListener('change',()=>{st().scope=scope.input.value;keptSetup(st());syncRun();});
  const keep=check('Keep existing assignments',keeping());
  keep.input.addEventListener('change',()=>{if(grouped())st().groupKeep=keep.input.checked;else st().keep=keep.input.checked;keptSetup(st());syncGroup();});
  // What a group run protects, said beside the choice before Run.
  const groupNote=el('p',{class:'hint',role:'status'});
  // A choice kept on this device that differs from the department's setting says so, with the way back: the run never
  // silently differs from one browser to the next.
  const keepSource=el('p',{class:'hint keep-source'});
  const syncKeep=()=>{const own=!grouped()&&st().keep!==undefined&&st().keep!==keepByDefault;keepSource.hidden=!own;
    keepSource.replaceChildren(...(own?[`Chosen on this device. The department setting ${keepByDefault?'keeps':'replaces'} existing assignments. `,
      button('Use the department setting',()=>{st().keep=undefined;keptSetup(st());syncGroup();},'link-button')]:[]));};
  const syncGroup=()=>{keep.input.checked=keeping();keep.input.disabled=grouped()&&!rebalanceGroup;groupNote.hidden=!st().group;syncKeep();
    groupNote.textContent=!st().group?'':keeping()?'Existing assignments stay as they are. Only these people are added.':'Only these people’s unlocked assignments may change. Everyone else’s stay.';};
  const people=scopeCap?peopleToSchedule({key:peopleKey,fields:ruleConditionFields(ctx()).filter(f=>scopeCap.fields.includes(f.key)),value:st().group,required:st().pickGroup,
    words:p=>conditionsSummary(p,namer(ctx()).label),onChange:(v,required)=>{Object.assign(st(),{group:v,pickGroup:required,scopeError:''});keptSetup(st());syncGroup();syncRun();}}):null;
  // A group asked for where the scheduler cannot take one: everyone only when chosen, never by itself.
  const noGroups:HTMLElement|null=!scopeCap&&grouped()?el('p',{class:'auto-stale',role:'status'},icon('alert'),'This scheduler cannot schedule only some people. ',
    button('Schedule everyone',()=>{Object.assign(st(),{group:null,pickGroup:false,scopeError:''});keptSetup(st());noGroups?.remove();syncGroup();syncRun();},'link-button')):null;
  syncGroup();
  // maintenance / D22: a group run leaves room for the other groups still to plan (on by default, slower); its result says
  // honestly whether a plan for them was shown possible (completionLine).
  // SX-23: the run follows the group shown (the scope button): All groups runs for the whole rota, a group you edit for it.
  const planning=planningOf(data),shownGroup=scopedGroup(w.id,planning);
  const own=shownGroup&&(shownGroup.is_mine||shownGroup.group_id===planning?.group_id||planning?.capabilities?.editable_group_ids?.includes(shownGroup.group_id))?shownGroup:null;
  const others=own?(planning!.groups||[]).filter(g=>g.group_id!==own.group_id).map(g=>g.name):[];
  // The joint lookahead needs CP-SAT (P2-BE LOOKAHEAD_ENGINE_REQUIRED): the basic scheduler runs the group alone.
  const leaveRoom=own&&others.length&&cpSat?check(`Leave room for ${others.length>2?'the other groups':new Intl.ListFormat('en').format(others)}`,st().leaveRoom??planning!.group_lookahead!=='none'):null;
  leaveRoom?.input.addEventListener('change',()=>{st().leaveRoom=leaveRoom.input.checked;});
  const validate=button(TERMS.runBackgroundCheck,()=>queue('validation'),'ghost small',{disabled:!editable,why:TERMS.cannotChangeRota});
  const syncValidate=()=>disabledWhy(validate,!editable?TERMS.cannotChangeRota:active?TERMS.runGoing:'');
  // Nothing to fill: Run stays disabled, and this says why with the way forward, before anyone presses it.
  const nothing=editable&&!live.some(s=>!s.locked)?el('p',{class:'auto-stale',role:'status'},icon('alert'),live.length?'Every session is locked, so there is nothing to fill. Unlock sessions on the schedule to fill them.':'This draft has no sessions yet. ',
    live.length?'':button('Add session',()=>shiftForm(ctx(),data),'link-button')):'';
  // The rules this run follows open beside the Schedule (Rules for this rota), not on another page that loses the run (G-05).
  // The basic scheduler weighs only mandatory rules: what it leaves out is said before the run, not only in its result (RUL-E05).
  const NOT_WEIGHED:Record<string,string>={wishes:'dated wishes',work_patterns:'work patterns',fairness_targets:'fairness targets',advisory_limits:'advisory limits'};
  const notWeighed=((capability.not_applied||[]) as Row[]).map(x=>NOT_WEIGHED[x.input]||String(x.input).replaceAll('_',' '));
  // What is recorded for the people already on the chosen sessions (on request; never blocks Run).
  const recorded=editable?readinessRegion({workspace:w.id,name,zone:zone(),check:()=>void command('workspace.validate',{reason:'Routine rota check'},w.id,w.revision).then(()=>refresh()).catch(reportError),input:()=>{const t=runScope(),g=grouped()?st().group:null;return typeof t==='string'?t:
    {expected_fingerprint:String(data.current_fingerprint),session_ids:t,...(g?{generation_scope:{conditions:g,protection:keeping()?'keep_existing' as const:'rebalance_selected' as const}}:{})};}}):null;
  // What changes what happens stays in sight above the setup's fields (D76: those open under Change): nothing to fill,
  // what the basic scheduler does not weigh, a group this scheduler cannot take, a choice kept on this device.
  const setupNotes=[nothing,!cpSat&&notWeighed.length?el('p',{class:'muted auto-basic'},`Basic scheduler · not weighed: ${notWeighed.join(', ')}`):'',people?'':noGroups||'',keepSource].filter(Boolean);
  // Stop when: one short line while every stop condition is the department's; the conditions open under it, and stay
  // open while one differs (a changed condition is never out of sight). Only CP-SAT has stop conditions.
  let stopOpen=stopsChanged();
  const stopLine=el('span',{class:'grow'}),stopMore=el('div',{class:'auto-numbers auto-stop-more'},...stops.inputs.map(f=>f.wrap));
  const stopWarn=el('p',{class:'hint auto-stop-warn'},icon('alert'),'Every break is a must-fix and blocks publishing until fixed.');
  const stopToggle=el('button',{type:'button',class:'link-button','aria-label':'Change stop conditions'},'Change');
  const paintStops=()=>{const changed=stopsChanged();stopOpen||=changed;stopLine.textContent=stopWords(Number(minutes.input.value)||Number(defaults.search_minutes)||AUTO_SCHEDULE_LIMITS.default_minutes,stops.read());
    stopMore.hidden=!stopOpen;stopToggle.hidden=changed;stopToggle.setAttribute('aria-expanded',String(stopOpen));stopToggle.textContent=stopOpen?'Done':'Change';
    stopWarn.hidden=stops.conflict.input.value!=='continue_flagged';};
  stopToggle.addEventListener('click',()=>{stopOpen=!stopOpen;paintStops();});
  for(const f of stops.inputs)f.input.addEventListener(f===stops.conflict?'change':'input',()=>{if(f.input.checkValidity())st().stops=stops.read();paintStops();});
  minutes.input.addEventListener('input',paintStops);paintStops();
  const stopGroup=cpSat?el('fieldset',{class:'auto-stops'},el('legend',{},'Stop when'),el('div',{class:'rl-done'},stopLine,stopToggle),stopMore,stopWarn):'';
  const fill=el('div',{class:'auto-fill'},el('div',{class:'form-grid'},dateRange('Dates',from,to)),clinics,people?.wrap||'',scope.wrap,keep.wrap,groupNote,leaveRoom?el('div',{class:'g-leave'},leaveRoom.wrap,el('small',{class:'muted'},'Slower')):'',recorded?.root||'',stopGroup,
    // Two different places (REQ-RULE-NAV): this rota's own adjustments open beside the Schedule; the department's Rules
    // is another page, a real link (Back returns here; a run keeps going and shows again on return).
    el('div',{class:'actions'},withIcon(button('Rules in this rota',()=>openRotaRules(),'ghost small'),'sliders'),
      allowed('rules')?el('a',{href:'#rules',class:'link-button'},'Open Rules →'):''));
  const range=()=>{const a=st().from,b=st().to,same=a.slice(0,7)===b.slice(0,7);
    return same?`${Number(a.slice(8))} – ${formatDate(b,zone())}`:`${formatDate(a,zone())} – ${formatDate(b,zone())}`;};
  const fillSummary=()=>[el('b',{},range()),` · ${st().clinics.length?st().clinics.map(id=>name('services',id)).join(', '):'all clinics'}`,
    ...(grouped()&&st().group?[' · ',scopeLine({conditions:st().group})]:[]),
    st().scope!=='period'?` · ${st().scope==='open'?'open sessions only':'selected sessions only'}`:'',keep.input.checked?' · keep existing assignments':'',
    cpSat&&stopsChanged()?` · stops ${stopWords(Number(minutes.input.value)||Number(defaults.search_minutes)||AUTO_SCHEDULE_LIMITS.default_minutes,stops.read()).replace(/^./,c=>c.toLowerCase())}`:''];

  // Priorities: real run overrides; score weights and wish budgets stay with department Configuration.
  // D76 (run card): the presets are one segmented choice (the contract's presets, arrow keys as every radiogroup); the
  // chosen one's weights are the line under it.
  const presetId='preset-label-'+w.id,weights=el('p',{class:'hint auto-weights'}),fairnessHelpId=presetId+'-help';
  const fairShare=RULE_CATALOGUE_V2.find(e=>e.id==='fair_share')!;
  const fairnessHelp=el('p',{class:'hint',id:fairnessHelpId},`${SCORE_WEIGHT_LABELS.fairness} balances duties only where a `,
    allowed('rules')&&ruleCatalogueOn()?el('a',{href:routeHash('rules',[fairShare.id]),class:'link-button'},fairShare.name):fairShare.name,
    ' rule applies.');
  const presets=el('div',{class:'segmented auto-presets',role:'radiogroup','aria-labelledby':presetId,'aria-describedby':fairnessHelpId},...PRESETS.map(p=>{
    const b=el('button',{type:'button',role:'radio','data-preset':p.key},p.label);
    b.addEventListener('click',()=>{st().preset=p.key;keptSetup(st());paintPresets();});return b;}));
  const paintPresets=()=>{for(const b of presets.querySelectorAll<HTMLElement>('[role=radio]')){const on=b.dataset.preset===st().preset;b.setAttribute('aria-checked',String(on));b.tabIndex=on?0:-1;}
    weights.textContent=weightSummary(defaults,st().preset);};
  paintPresets();
  const priorities=cpSat?el('div',{class:'auto-run-options'},el('span',{class:'pick-label',id:presetId},'Priorities'),presets,weights,fairnessHelp,el('div',{class:'auto-numbers'},minutes.wrap,fairness.wrap),
      permits(CAPABILITY.ADMIN)?withIcon(button('Change scoring settings',()=>navigate('configuration/auto_schedule'),'ghost small'),'settings'):''):'';
  const presetName=()=>PRESETS.find(p=>p.key===st().preset)?.label||PRESETS[0].label;
  /** A run under review is said as it was run (its job: who, what it kept, its priorities), never as the setup now:
   *  the setup may have changed since. Its dates and clinics are the duties it actually had (run_context: the selected
   *  duties' extent and their clinics); an older run without that record claims none. */
  const reviewedSummary=(j:Row)=>{const scope=j.generation_scope,o=j.effective_options||{},ctxt=j.run_context as JobRunContext|null|undefined,sel=ctxt?.selection_window;
    const keptOld=scope?scope.protection==='keep_existing':o.mode?o.mode==='fill_gaps':undefined;
    const days=sel?` · duties ${formatDate(sel.start,zone())} – ${formatDate(new Date(Date.parse(sel.end)-1).toISOString(),zone())}`:'';
    // ME-19: a run over every clinic of the rota says "all clinics" (as What to fill does); a long list says how many.
    const ran=ctxt?.service_ids||[],every=services.length>0&&services.every(x=>ran.includes(x.value));
    const clinics=!ran.length||every?' · all clinics':ran.length>3?` · ${plural(ran.length,'clinic')}`:` · ${ran.map(id=>name('services',id)).join(', ')}`;
    return [el('b',{},`Run of ${stamp(j.created_at)}`),days,clinics,' · ',scopeLine(scope),
      keptOld===undefined?'':keptOld?' · kept existing assignments':' · could change existing assignments',
      cpSat&&o.weight_preset?` · ${PRESETS.find(p=>p.key===o.weight_preset)?.label||PRESETS[0].label}`:cpSat&&j.effective_options?` · ${PRESETS[0].label}`:''];};

  // 3 · Review & apply: progress while it runs, then the review (filled in by load()).
  const feedback=el('div',{class:'scheduling-result','aria-live':'polite'}),history=el('div',{class:'scheduling-history'});
  // A redraw (Show proposal, a live update) builds a new panel: until its first read is back it shows the last result,
  // inert and marked busy (the shared top progress bar runs for the read), never a blank panel.
  // The copy is a picture of the last result: no ids or rule-target marks (Show on schedule marks the real ones).
  const still=(n:Node)=>{const c=n.cloneNode(true);if(c instanceof Element)for(const e of [c,...c.querySelectorAll('[id],[data-rk]')]){e.removeAttribute('id');e.removeAttribute('data-rk');}return c;};
  if(lastSchedulingBody?.id===w.id){feedback.append(...lastSchedulingBody.feedback.map(still));history.append(...lastSchedulingBody.history.map(still));
    feedback.inert=history.inert=true;feedback.setAttribute('aria-busy','true');}
  const sub=el('small',{},w.name),body=el('div',{class:'auto-body'}),foot=el('div',{class:'auto-foot'});
  // D76 (run card): no steps. Setup is one line with Change (the dates, clinics, people and sessions open under it),
  // the priorities and the two search numbers; the same card then shows the run and its result.
  // The setup fields stay open (Change) across the panel's redraws, as its step does: st(), not this draw.
  const closeButton=iconButton('Close auto-schedule','close',()=>closePanel?.(),{class:'bare'});
  const panel=el('div',{class:'automatic-scheduling'},el('div',{class:'auto-head'},el('span',{class:'auto-icon','aria-hidden':'true'},icon('sparkle')),el('div',{},el('h2',{},'Auto-schedule'),sub),closeButton),body,foot);
  // Early findings change rarely: the same list keeps its node (and what was opened in it) across the run's reads.
  let early={key:'',node:null as HTMLElement|null};
  const earlyIssues=(job:Row)=>{const found=Array.isArray(job.progress?.precheck_issues)?(job.progress.precheck_issues as Row[]).filter(i=>i&&typeof i==='object'&&i.code):[];if(!found.length)return '';
    const key=job.id+JSON.stringify(found);if(early.key!==key)early={key,node:precheckIssues(data,found)};return early.node!;};
    // reasons: the places a run found nobody can fill, kept on screen while the chosen "Fill everything else" runs.
  let active=false,review:Row|null=null,continuation:HTMLButtonElement|null=null,shortfall:Row|null=null,staleNote='',reasons:HTMLElement|null=null;
  // The "fill everything else" run the person chose after a run found places nobody can fill: its reasons stay.
  const continuedNotes=(diagnostic:boolean)=>diagnostic?[el('p',{class:'hint'},'Some places cannot be filled, so this run fills everything else and leaves those open for you.'),
    ...(reasons?[reasons]:[])]:[];
  // The running run, its Stop in view: what a second press shows instead of starting another.
  const showRun=()=>{st().step=3;st().open=true;target.hidden=false;document.querySelector('.auto-button')?.setAttribute('aria-expanded','true');drawStep();
    (feedback.querySelector<HTMLElement>('[data-run-stop]:not(:disabled)')??feedback.querySelector<HTMLElement>('.auto-stage'))?.focus();};
  // `extra`: options for this one run beyond the setup (Fill everything else without a checkpoint: on_conflict).
  const queue=async(kind:string,diagnostic=false,extra?:Partial<GenerationOptions>)=>{
    // One run per rota: a second press (double click, a redrawn panel, a slow server) shows the run already going.
    if(active||st().starting){showRun();return;}
    let ids:string[]=[];
    if(kind==='generation'){
      const bad=cpSat?[minutes,fairness].find(f=>!f.input.checkValidity()):undefined;if(bad){st().step=1;drawStep();bad.input.reportValidity();return;}
      const badStop=cpSat?stops.inputs.find(f=>!f.input.checkValidity()):undefined;if(badStop){st().step=1;st().editing=stopOpen=true;paintStops();drawStep();badStop.input.reportValidity();return;}
      if(!from.input.validity.valid||!to.input.validity.valid){st().step=1;st().editing=true;drawStep();from.input.reportValidity()&&to.input.reportValidity();return;}
      const t=runScope();if(typeof t==='string')throw new Error(t);ids=t;
    }
    // Feedback at once: Review & apply and the footer show the run starting before the server has answered.
    st().starting=kind;let starting:HTMLElement|null=null;
    if(kind==='generation'){st().step=3;drawStep();feedback.inert=false;feedback.removeAttribute('aria-busy');
      feedback.replaceChildren(...continuedNotes(diagnostic),generationProgress({id:'',revision:0,status:'queued'},0,diagnostic,phoneQuery.matches));starting=startingStrip();void liveView();}
    else drawStep();
    let created:Row;
    try{created=await command('job.create',{workspace_id:w.id,kind,diagnostic_proposal:diagnostic,...(kind==='generation'&&cpSat&&(overridden()||extra)?{generation_options:{...runOptions(),...extra}}:{}),
      // The greedy scheduler takes the run's keep/replace choice as its mode too (the server honours an explicit mode).
      ...(kind==='generation'&&!cpSat?{generation_options:{mode:keeping()?'fill_gaps':'rebalance'} as GenerationOptions}:{}),
      // A group run: who it schedules and how everyone else's placements are protected (the server checks both).
      ...(kind==='generation'&&grouped()?{generation_scope:{conditions:st().group,protection:keeping()?'keep_existing':'rebalance_selected'}}:{}),
      // P2-BE: a run for your scheduling group, with the joint lookahead for the others (D22).
      ...(kind==='generation'&&own?{planning_group_version_id:own.group_version_id||(own.group_id===planning!.group_id?planning!.group_version_id:undefined),lookahead:leaveRoom?.input.checked?'coupled':'none'}:{}),
      // Repairs move other people's assignments: never in a group run.
      session_ids:ids,repair_unavailable:!cpSat&&kind==='generation'&&!keeping()&&!grouped(),repair_conflicts:!cpSat&&kind==='generation'&&!keeping()&&!grouped()},undefined,w.revision);}
    catch(e){st().starting='';starting?.remove();jobsKey='';
      // A group the server refuses (nobody matches, a field it no longer accepts): said on What to fill, beside Run.
      if(e instanceof ApiError&&e.code==='INVALID_SCOPE'){st().scopeError=e.message;st().step=1;drawStep();void load().catch(failed);return;}
      // SW3-A-03: a run that did not start says so, with Try again; Run is offered again. The read after it may fail
      // too under load: that is not news here (it said "Showing the last result" when there was none).
      drawStep();if(sessionChanged(e))return;feedback.replaceChildren();await load().catch(()=>undefined);
      if(!feedback.isConnected||active)throw e;
      feedback.querySelector('.auto-start-failed')?.remove();
      if(e instanceof ApiError&&e.code==='SOLVER_RULE_INCOMPATIBLE'){const n=runErrorNode(e.code,e.details,'');n.classList.add('auto-start-failed');feedback.prepend(n);return;}
      const n=note(`The run didn't start. ${(e as Error).message}`,'error',e);n.classList.add('auto-start-failed');
      n.append(' ',button('Try again',()=>{n.remove();void queue(kind,diagnostic,extra).catch(reportError);},'link-button'));
      feedback.prepend(n);return;}
    st().starting='';starting?.remove();active=true;
    // The new run is on the grid and in the footer now, not after the next read of the job list.
    const job=created?.job as Row|undefined;
    if(job?.id&&kind==='generation'){st().live=job.id;const hooks=liveHooks(data);void ours(liveView()).then(m=>m.liveRun(job.id,hooks).update(job));}
    st().step=3;drawStep();
    await load();
    // A view change may have replaced this panel before the queued job reached the server.
    if(serial!==renderSerial&&view==='schedule'&&workspaceId===w.id)await renderPage();
  };
  // The sessions a run fills ([] = the whole rota), or why it cannot run with these choices (SR-28: said before Run is pressed).
  function runScope():string[]|string{
    const a=st().from,b=st().to;
    if(grouped()&&!scopeCap)return 'This scheduler cannot schedule only some people.';
    if(grouped()&&!st().group)return 'Choose who to schedule.';
    if(!a||!b)return 'Choose the first and last day.';
    if(a>b)return 'The first day must be before the last.';
    if((Date.parse(b)-Date.parse(a))/864e5+1>maxDays)return `Choose at most ${maxDays} days at a time.`;
    const inRange=live.filter(s=>!s.locked&&day(s)>=a&&day(s)<=b&&(!st().clinics.length||(s.service_ids as string[]).some(id=>st().clinics.includes(id))));
    const whole=a===first&&b===last&&!st().clinics.length;
    const ids=st().scope==='selected'?inRange.filter(s=>selected.has(s.id)).map(s=>s.id):st().scope==='open'?inRange.filter(s=>openIds.has(s.id)).map(s=>s.id):whole?[]:inRange.map(s=>s.id);
    if(st().scope==='selected'&&!selected.size)return 'Select sessions on the schedule first.';
    if(st().scope==='selected'&&!ids.length)return 'No selected sessions in these dates and clinics.';
    if(st().scope==='open'&&!ids.length)return 'No open places in these dates and clinics.';
    if(!whole&&!ids.length)return 'No sessions in these dates and clinics.';
    return ids;
  }
  const runnable=()=>editable&&!active&&!st().starting&&live.some(s=>!s.locked)&&typeof runScope()!=='string';
  const run=withIcon(button(TERMS.runAutoSchedule,()=>queue('generation'),'primary'),'play');
  const runWhy=el('span',{class:'grow muted auto-why',role:'status'});
  // Review maintenance: why Run (and Fill everything else) can't start, always in words; the scope's own words first.
  const runReason=()=>{if(runnable())return '';const w=editable&&!active?runScope():'',said=typeof w==='string'?w:st().scopeError;
    return said||(!editable?TERMS.cannotChangeRota:active||st().starting?TERMS.runGoing:live.length?'Every session is locked, so there is nothing to fill.':'This draft has no sessions, so there is nothing to fill.');};
  const syncRun=()=>{const w=editable&&!active?runScope():'',said=typeof w==='string'?w:st().scopeError;disabledWhy(run,runReason(),[],runWhy);if(!run.disabled)runWhy.textContent=said;};
  autoRunSync=()=>{if(panel.isConnected)syncRun();else autoRunSync=null;};
  function drawStep(){
    // ME-18b: until the latest run is known, its step is not (a result may open on Review & apply): no Run to press.
    // A closed panel waits on nothing (its results are read when opened), so it holds no skeleton: one there kept saying
    // "Still loading · m:ss" (and announcing it) for as long as the page stayed open (maintenance J5, 11 Nov 2026).
    if(!st().step&&!known){body.replaceChildren(...target.hidden?[]:[skeleton('panel','Loading auto-schedule…')]);foot.replaceChildren();foot.hidden=true;return;}
    const step=st().step===3?3:1;recorded?.sync();paintPresets();
    body.dataset.state=step===1?'setup':'run';  // the run card's two faces: its setup, or its run and result
    // Setup that cannot run as chosen (a group to pick, a refused group, dates that do not fit) opens its fields.
    if(step===1&&(st().pickGroup||st().scopeError))st().editing=true;
    // The setup is one line with Change. On setup, Change opens the fields under it; on a run or its result (said as
    // it was run), Change goes back to setup.
    const line=(parts:(string|Node)[],open:()=>void,label='Change')=>{const change=el('button',{type:'button',class:'link-button','aria-expanded':step===1?String(st().editing):null},label);
      change.addEventListener('click',open);return el('div',{class:'rl-done'},el('span',{class:'grow'},...parts),change);};
    if(step===1)body.replaceChildren(line(fillSummary(),()=>{st().editing=!st().editing;drawStep();},st().editing?'Done':'Change'),...(staleNote?[el('p',{class:'auto-stale',role:'status'},icon('alert'),staleNote)]:[]),
        ...setupNotes,...(st().editing?[fill]:[]),priorities,...(active||st().starting?[el('p',{},button('Show run',showRun,'link-button'))]:review||feedback.querySelector('.auto-outcome')?[el('p',{},button('Back to the result',()=>{st().step=3;drawStep();},'link-button'))]:[]));
    else{
      const summary=line(review?reviewedSummary(review):[...fillSummary(),cpSat?` · ${presetName()}`:''],()=>{st().step=1;st().editing=true;drawStep();});
      if(feedback.parentNode===body)body.firstChild!.replaceWith(summary);
      else body.replaceChildren(summary,feedback,history);
    }
    // The footer holds the one next move: Run; for a proposal Apply (Discard is a quiet link); for a run that stopped
    // because some places cannot be filled, Fill everything else.
    const n=review?changeCount(review.result):0;
    if(step===3&&review&&editable&&n){
      const job=review;
      const discard=armed(button('Discard',async()=>{
        // Saved with the job (job.dismiss, audited: maintenance), so a reload or another browser shows it discarded too. A server
        // without that command keeps the discard in this browser session, as before.
        await command('job.dismiss',{reason:'Discard this proposal'},job.id,job.revision).catch(e=>{if(!(e instanceof ApiError&&(e.code==='UNKNOWN_COMMAND'||e.status===422)))throw e;});
        dismissedJobs.add(job.id);if(proposal?.job_id===job.id)proposal=null;jobsKey='';void liveModule?.then(m=>m.endFinished());await renderPage();},'link-button'),'Discard?');
      // A4: what still fits after the rota changed is applied (the server checked it again); a worse run (a new must-fix
      // cause, A3) makes Discard the primary.
      const f=runFacts(job),kept=inputsChanged(job,f,{person:()=>'',session:()=>''}),worse=!!f.after&&!!f.before&&f.after.must>f.before.must;
      const apply=button(kept?`Apply ${plural(kept.kept,'change')}`:rechecked(job)?'Check again and apply':`Apply ${plural(n,'change')}`,async()=>{
        await applyJob(job,kept?{addition_indexes:kept.additions,removal_indexes:kept.removals}:undefined);await refresh();},worse?'secondary':'primary');
      if(worse)discard.className='button primary';
      foot.replaceChildren(discard,el('span',{class:'grow hint'},'You can undo'),...(kept?[button('Run again with the changes',()=>queue('generation'),'link-button')]:[]),apply);
    }else if(step===3&&continuation)foot.replaceChildren(validate,el('span',{class:'grow'}),continuation);
    else if(step===3&&shortfall&&editable&&cpSat)foot.replaceChildren(validate,el('span',{class:'grow'}),button('Fill everything else',()=>fillRest(shortfall!),'primary',{disabled:!runnable(),why:runReason(),'data-tip':'Fills every place that can be filled; the rest stays open for you.'}));
    // A run going: the earlier steps lead back to it (its progress and Stop), never to a second run.
    // ME-03: on Review & apply its progress holds Stop and Cancel, so the footer offers no second Run or check.
    else if(active||st().starting)foot.replaceChildren();
    else foot.replaceChildren(validate,runWhy,run);
    syncRun();syncValidate();foot.hidden=!foot.childElementCount;
  }
  if(sk.parentNode===target)sk.replaceWith(panel);else target.append(panel);
  if(panel.isConnected)trackStatusFooter(document.querySelector<HTMLElement>('.app-footer'));
  let timer:number|undefined;
  let jobsKey='',loads=0,wasRunning='',queuedPolls=0,polling=false,known=!!st().step;
  let cancelRun:{job:Row;row:HTMLElement;spacer:HTMLElement}|undefined;
  // A refresh that failed keeps the result on screen under one line, Retry inside; Apply waits for a fresh read (G-46).
  const failed=(e:unknown)=>{if(sessionChanged(e)||!panel.isConnected)return;
    // Nothing shown yet: the panel's wait ends in the failure, with Retry, never in a skeleton left loading (DL-045).
    if(!known&&!st().step){body.replaceChildren(readFailed('auto-schedule',e,()=>{drawStep();void load().catch(failed);}));return;}
    feedback.querySelector('.auto-paused')?.remove();
    // A refresh failure keeps the panel's last state, including a run still in progress; a failed START takes the
    // separate "The run didn't start" path in queue() and never reaches this note.
    const said=errorWords(e),n=note(`Couldn't refresh. Showing the last result.${said?' '+said:''}`,'warning');n.classList.add('auto-paused');
    n.append(' ',button('Retry',()=>{n.remove();void load().catch(failed);},'link-button'));
    // Forget the drawn key: the next good read redraws the footer (Apply enabled) even when its jobs are unchanged.
    jobsKey='';feedback.prepend(n);for(const b of foot.querySelectorAll<HTMLButtonElement>('button.primary'))b.disabled=true;};
  // Back on the tab with a run going: read it at once instead of waiting out a slower poll.
  // One listener at a time: a redrawn panel replaces the previous one's, so an old panel (and the page around it) is not
  // kept alive by a tab that never changes visibility (perf SC-01).
  // A panel in the page still being built (alive) is the one that will show: it reads too, or the wake-up is lost.
  const onVisible=()=>{if(!alive(panel))return;if(!document.hidden&&(polling||!target.hidden))void load().catch(failed);};
  if(panelVisibility)document.removeEventListener('visibilitychange',panelVisibility);
  panelVisibility=onVisible;document.addEventListener('visibilitychange',onVisible);
  // One polling chain: a newer load (panel opened, Retry, cancel) supersedes one still waiting for the server.
  // After every await the live view is still this panel's: same render, same rota, panel on the page. A switch to another
  // rota meanwhile must not end that rota's run nor put this one's result (and its Apply) over it.
  // H-02: a newer draw of the page that has not landed yet (it waits on the rota's read, minutes under load) does not
  // silence the panel still on screen: only a panel taken off the page is gone. (Keyed on the draw, the panel on screen
  // stopped drawing its reads, so the progress froze while it kept reading every second.)
  const gone=()=>serial!==renderSerial&&!panel.isConnected;
  const ours=whileFresh(()=>workspaceId===w.id&&alive(panel));
  const finishLive=async(id:string)=>{const job=(await ours(get('/api/jobs/'+id))).job,m=await ours(liveView()),hooks=liveHooks(data);
    const r=job.result,summary=r?.summary||{},changes=(r?.additions||[]).length+(r?.removals||[]).length;
    m.liveRun(id,hooks).finish(job,target.hidden&&job.status==='succeeded'&&r?{gain:scoreDelta(summary.score_before,summary.score_saved??summary.score),
      review:()=>{st().open=true;st().step=3;target.hidden=false;document.querySelector('.auto-button')?.setAttribute('aria-expanded','true');jobsKey='';void load().catch(failed);},
      apply:editable&&changes&&currentGeneration(job,data)?async()=>{await applyJob(job);await refresh();}:null}:null);};
  // The live stream wakes a watched run at once (polling stays the fallback).
  panelPulse(e=>{const ids=(e as CustomEvent<string[]|null>).detail;if(alive(panel)&&polling&&st().live&&!document.hidden&&(!ids||ids.includes(st().live)))void load().catch(failed);});
  const finishShown=liveFinisher(st(),finishLive);
  const liveRead=async(id:string):Promise<Row>=>{const status=(await get('/api/jobs/'+id+'/status?events=true')).job as Row,
    job=DONE_JOB.includes(status.status)?(await get('/api/jobs/'+id)).job as Row:status,list=lastJobs!.jobs;
    return {...list,jobs:(list.jobs as Row[]).map(j=>j.id===id?job:j)};};
  // Fill everything else after a run that hit rules that cannot all be met: the server continues it from its saved
  // preparation with every break flagged (job.continue → on_conflict 'continue_flagged'); without a checkpoint (expired,
  // an older run), a new run with the same options does the same. No confirm: the press is the choice, its cost is on the
  // card, and nothing is applied before Review; the run's history records the choice as its reason.
  const fillRest=async(conflicted:Row)=>{
    if(!conflicted.result?.resume_available)return queue('generation',true,{on_conflict:'continue_flagged'});
    const continued=await command<Row>('job.continue',{reason:'Chosen: Fill everything else (flag every break)'},conflicted.id).catch(e=>supersededRun(e,w.id));
    if(continued.preparation_expired)notify('Cached preparation expired. This attempt rebuilds from your saved inputs.','info');await load();};
  // Continue a run from its saved checkpoint (job.continue) for the minutes chosen; nothing is applied until reviewed.
  const resumeControls=(latest:Row,tone:string)=>{const saved=st().resume;
    const more=timeField('search_minutes',saved&&saved.job===latest.id?Number(saved.minutes):Number(latest.effective_options?.search_minutes)||defaults.search_minutes,{label:'More search time',max:maxMinutes});
    const said=()=>{const n=more.value();return Number.isFinite(n)?duration(n*60):'…';};  // the time as people read it
    const go=button(`Continue for ${said()}`,()=>{if(!more.input.reportValidity())return;
      confirmAction('Continue auto-schedule','Continue from the saved inputs, reusing cached preparation when available. Nothing is applied to the draft until you review the proposal.',async reason=>{
        const continued=await command<Row>('job.continue',{minutes:more.value(),reason},latest.id).catch(e=>supersededRun(e,w.id));
        if(continued.preparation_expired)notify('Cached preparation expired. This attempt rebuilds from your saved inputs.','info');await load();},'Continue');},tone,{'data-run-continue':''});
    more.input.addEventListener('input',()=>{st().resume={job:latest.id,minutes:String(more.value())};go.textContent=`Continue for ${said()}`;});
    return {more,go};};
  const load=async(firstRead?:Row,since=panelWakes):Promise<void>=>{
    window.clearTimeout(timer);const mine=++loads,woke=firstRead?since:panelWakes,applyEpoch=generationApplyEpoch;
    // Opened before anything was read: its skeleton shows while this read is on its way.
    if(!known&&!st().step&&!target.hidden&&!body.querySelector(':scope>.loading'))drawStep();
    // A run shown live is read on its own (its progress) while it goes, not the rota's whole job list every second;
    // the list is read again once it has ended (or when this panel has none yet).
    const liveId=!firstRead&&st().live&&lastJobs?.id===w.id&&(lastJobs.jobs.jobs as Row[]).some(j=>j.id===st().live)?st().live:'';
    const response:Row=withEnded(firstRead||(liveId?await liveRead(liveId):await get('/api/jobs?workspace_id='+encodeURIComponent(w.id))));
    // A Run press still waiting for the server shows its run starting; a reply read meanwhile has no run in it yet and
    // must not draw over it ("No auto-schedule runs"). The press reads the jobs again once the server has answered.
    if(gone()||mine!==loads||applyEpoch!==generationApplyEpoch||st().starting)return;
    // Back on the tab since this read started: it may predate what changed (a run started meanwhile). Read again at once,
    // never drawing it: a stale draw is not harmless (a result shown superseded sends the panel back to its setup).
    if(woke!==panelWakes)return load();
    lastJobs={id:w.id,jobs:response,activity:seenActivity,woke};markProposal(data);
    const jobs:Row[]=response.jobs.filter((j:Row)=>j.kind!=='feasibility'),running:Row|undefined=response.jobs.find((j:Row)=>['queued','running'].includes(j.status));
    // Polled only while a run is queued or running (PL-03): every 3 s while it is watched, every 30 s while the panel is
    // closed or the tab hidden (so a finish is still said), a queued run backing off 3 → 10 → 30 s (restarted by your own
    // Stop or Add time on it: their outcome is read within 3 s, not after a 30 s wait). A rota with no run
    // going is read every 30 s only while its panel is open and the tab shown: another coordinator's new run does not
    // move the live pulse (job.create changes no rota revision), so an idle open panel would never see it.
    // shortcut: idle open panels poll every 30 s; a job-change pulse from the server would remove this poll.
    // A run going opens the panel (before its poll is timed: an opened panel is watched).
    // Once per run: closing the panel while it runs keeps it closed (then polled every 30 s).
    // D76: a check queued or running is the footer's working item (with Cancel), never this panel's.
    const check:Row|undefined=editable?response.jobs.find((j:Row)=>j.kind==='validation'&&['queued','running'].includes(j.status)):undefined;
    if(check)autoCheck(data,check);
    // A run going opens the panel once (a check never does: it is the footer's, D76).
    const liveRunning:Row|undefined=response.jobs.find((j:Row)=>j.kind==='generation'&&['queued','running'].includes(j.status));
    if(liveRunning&&target.hidden&&closePanel&&st().seen!==liveRunning.id){st().open=true;target.hidden=false;document.querySelector('.auto-button')?.setAttribute('aria-expanded','true');}
    queuedPolls=running?.status==='queued'?queuedPolls+1:0;polling=!!running;
    // A generation run is shown live on the grid and in the footer, panel open or not. While it searches, the live stream
    // wakes this read when the worker reports (every second); without the stream it is read every second instead.
    watchPanelJob(liveRunning?.id||'');
    const searching=liveRunning?.status==='running';
    const watched=(!target.hidden||searching)&&!document.hidden,delay=!running?(watched?30000:0):!watched?30000:running.status==='queued'?[3000,10000,30000][Math.min(queuedPolls,3)-1]:searching&&Date.now()-streamSeen>=45_000?1000:3000;
    if(delay)timer=window.setTimeout(()=>{if(alive(panel))void load().catch(failed);},delay);
    const compiler=response.generation_capability?.algorithm;
    const nextKey=JSON.stringify([jobs,running,compiler]);
    // A run that finishes while you work elsewhere (panel closed, another tab) says so once.
    // (The live view's finish card says it instead, when the run was shown on this schedule.)
    if(wasRunning&&!liveRunning&&(document.hidden||target.hidden&&!st().live))notify(`Auto-schedule finished for ${w.name}. Open Auto-schedule to review it.`,'info');
    if(liveRunning){st().live=liveRunning.id;const hooks=liveHooks(data);void ours(liveView()).then(m=>m.liveRun(liveRunning.id,hooks).update(liveRunning));}
    // #1160: the run's own end ends its footer strip; a check beside it (or one a live read's kept list still shows
    // running) never holds it on its last step.
    else finishShown();
    wasRunning=liveRunning?.id||'';
    // A run shown live has its progress in the footer: no second "working" line over the schedule.
    // D76: only a run makes the panel busy; a check runs beside it (the server allows one run per draft, any checks).
    active=!!liveRunning;target.closest('.planner-body')?.classList.toggle('is-working',!!running&&!liveRunning||autoChecking);syncRun();syncValidate();
    // Hidden panels still discover live jobs, but completed results are read only when opened.
    if(!running&&target.hidden){if(!jobs.length)known=true;if(!st().step)drawStep();return;}
    if(!liveRunning&&nextKey===jobsKey)return;
    // A Run press still waiting for the server: a list read before it (none yet, or older runs) never replaces its pending
    // progress; the press reads again once the run is created. A panel rebuilt meanwhile shows that progress too.
    if(st().starting&&!liveRunning){if(st().starting==='generation'&&!feedback.querySelector('.auto-stage')){feedback.inert=false;feedback.removeAttribute('aria-busy');
      feedback.replaceChildren(generationProgress({id:'',revision:0,status:'queued'},0,false,phoneQuery.matches));}return;}
    // The latest run is read before anything is cleared: what is shown stays until its replacement is ready.
    // Drawing the proposal reuses the read already on screen; fresh summaries still verify its job and rota revisions.
    // A rule check does not replace an unfinished auto-schedule run's Continue action.
    // The rota check that follows an Apply (maintenance) does not take the applied run's place: the panel keeps saying what the
    // run applied (with its Undo), or, once undone or edited after, that it is out of date; the check is in Recent runs
    // and the footer. D76: a check still running never takes the panel either; it is the footer's working item with its
    // Cancel. Only a running auto-schedule shows its progress here.
    const settled=jobs.filter(j=>j.kind==='generation'||DONE_JOB.includes(j.status));
    const run=settled.findIndex(j=>j.kind!=='validation');
    const confirmed=run>0&&settled.slice(0,run).every(j=>DONE_JOB.includes(j.status))&&settled[run].kind==='generation'
      &&settled[run].result?.applied_content_revision!=null;
    const generation=settled.find(j=>j.kind==='generation'),shown=generation?.outcome==='needs_more_time'&&generation.status==='succeeded'&&
      !currency(generation,data.current_fingerprint)?generation:confirmed?settled[run]:settled[0];
    const cached=!liveRunning&&shown&&proposal?.job_id===shown.id&&proposal.session===sessionKey()&&proposal.workspace_revision===w.revision&&proposal.job.revision===shown.revision&&!notCompared(proposal.job)&&proposal.job.input_fingerprint===data.current_fingerprint?proposal.job:null;
    const applied=shown?.status==='applied'&&shown.result?.applied?shown:null;
    const latestRead:Row|null=!liveRunning&&shown?(applied||cached||(shown.kind==='validation'?heldCheck(String(shown.id)):null)||(await get('/api/jobs/'+shown.id)).job):null;
    if(gone()||mine!==loads||applyEpoch!==generationApplyEpoch||st().starting)return;
    // Focus inside the run's progress or result stays on the same control across this redraw (WCAG 2.4.3).
    const had=feedback.contains(document.activeElement)?document.activeElement:null,keep=had?focusSpot(feedback,had):null;
    // Keep Cancel mounted for this run so a status update cannot take away the press or its confirmation.
    if(cancelRun?.job.id!==liveRunning?.id)cancelRun=undefined;
    for(const node of [...feedback.childNodes])if(node!==cancelRun?.row)node.remove();
    history.replaceChildren();feedback.inert=history.inert=false;feedback.removeAttribute('aria-busy');review=null;continuation=null;shortfall=null;let latestRun:Row|null=null;
    if(liveRunning){const running=liveRunning;
      // F-12: a run seen while the panel is open never moves the step the person is on (its own Run already went to
      // Review & apply); only a panel opened on nothing goes straight to the run. Earlier steps offer Show run.
      if(st().seen!==running.id){st().seen=running.id;if(!st().step)st().step=3;}
      const elapsed=Math.max(0,Math.floor((Date.now()-new Date(running.started_at||running.created_at).getTime())/1000));
      const checkingRules=running.kind!=='generation';
      // SX-20 (SW-16): one clock — the progress below says the time; the head says only the state.
      sub.textContent=`${w.name} · ${running.status==='queued'?'queued':'running'}`;
      // Filling everything else: the first run's reasons stay above its progress (SR-26).
      const progress=document.createDocumentFragment();
      if(!checkingRules)progress.append(...continuedNotes(!!running.diagnostic_proposal));
      // Issues first: what the run's checks found before the search (job.progress.precheck_issues), at once.
      progress.append(earlyIssues(running));
      // Queued: no claim that a worker is alive or how long it takes; cancelling is always safe (CA-07).
      if(running.status==='queued'){const ops=allowed('system')&&tabsFor('system').some(t=>t.key==='operations');
        progress.append(el('p',{class:'muted'},`Queued. Cancelling leaves the draft unchanged.${ops?' ':' If it stays queued, ask an operator to check the scheduler.'}`,
          ops?el('a',{href:'#system/operations',class:'link-button'},'Scheduler status'):''));}
      progress.append(checkingRules?el('p',{role:'status'},running.status==='queued'?'Waiting to start…':`Checking rules · ${elapsed}s elapsed`):generationProgress(running,elapsed,running.diagnostic_proposal,phoneQuery.matches,defaults),
        // maintenance: edits made meanwhile no longer end the run; the finished proposal is checked against them (A4).
        ...(!checkingRules&&running.status==='running'?[el('p',{class:'muted auto-keep-working'},'You can keep working. Edits are checked against the result when it finishes.')]:[]),
        ...(checkingRules?[el('progress',{'aria-label':'Rule check in progress'})]:[]));
      feedback.prepend(progress);
      // D76: a queued run offers only Cancel; Stop and keep best and more time once its search is running. Owner (7 Nov
      // 2026): Stop and keep best is the primary (it keeps the best schedule for review); Cancel discards, so it is the
      // same-button armed confirm; each says what it does in its "?"; Add time only when minutes can be added.
      if(!checkingRules&&cpSat){
          const stopping=!!running.search_control?.stop,searching=running.status==='running'&&!GENERATION_SEARCH_FINISHED_STAGES.includes(running.progress?.stage);
          const room=addTimeRoom(running,maxMinutes,Number(defaults.search_minutes)),step=addTimeStep((me!.department as Row).display_settings?.generation_controls,room);
          const stop=button(stopping?'Keeping the best result…':'Stop and keep best',()=>confirmAction('Stop and keep best','Finish with the best proposal found. If none is ready yet, you can continue from the saved inputs; preparation that had not finished runs again.',async reason=>{await command('job.stop_keep_best',{reason},running.id);queuedPolls=0;await load();},'Stop and keep best'),'primary',{disabled:!editable||stopping,why:!editable?TERMS.cannotChangeRota:TERMS.keepBestStopped,'data-run-stop':''});
          // How much time to add is chosen per press, up to the run's ceiling (job.add_time takes any bounded amount); the
          // suggested step is the department's (Configuration › Auto-schedule & scoring › Timing).
          const add=editable&&searching&&!stopping&&room>=AUTO_SCHEDULE_LIMITS.minimum_minutes?button(`Add ${duration(step*60)}`,()=>{const more=timeField('extension_minutes',step,{label:'Time to add',max:room});
              confirmAction('Add search time',`Give this search more time (up to ${duration(room*60)}). Preparation and final checks do not use this time.`,async reason=>{await command('job.add_time',{minutes:more.value(),reason},running.id);queuedPolls=0;await load();},'Add time',more.wrap);},'ghost'):'';
          if(!cancelRun){
            const state={job:running,row:el('div',{class:'actions auto-run-actions'}),spacer:el('span',{class:'grow'})};
            const cancel=armed(button('Cancel',async()=>{await command('job.cancel',{reason:'Cancelled from the Auto-schedule panel'},state.job.id,state.job.revision);await load();},'ghost run-cancel',{disabled:!editable,why:TERMS.cannotChangeRota,'data-run-stop':'','aria-label':'Cancel auto-schedule'}),'Discard this run?');
            state.row.append(state.spacer,cancel,helpButton('About Cancel','Stops the run and discards it. The draft stays as it is.'));
            cancelRun=state;
          }
          const {row,spacer}=cancelRun;cancelRun.job=running;
          while(row.firstChild!==spacer)row.firstChild!.remove();
          if(running.status!=='queued')spacer.before(stop,helpButton('About Stop and keep best','Ends the search now and keeps the best schedule found so far for you to review. Nothing changes until you apply it.'),...(add?[add]:[]));
          if(row.parentNode!==feedback)feedback.append(row);
      }else feedback.append(el('div',{class:'actions'},button(checkingRules?'Cancel check':'Cancel auto-schedule',()=>confirmAction(checkingRules?'Cancel check':'Cancel auto-schedule','Keep the draft unchanged and discard this job’s result.',async reason=>{await command('job.cancel',{reason},running.id,running.revision);await load();},'Confirm',undefined,true),'secondary',{'data-run-stop':''})));
    }else if(latestRead){
      const latest=latestRead;latestRun=latest;
      const result=latest.result;
      const labels:Record<string,string>={needs_more_time:'More search time needed',candidate_feasible:result?.validation?'Ready for review':'No candidate validation available',requirements_conflict:'Some places cannot be filled',model_invalid:'Scheduling needs attention',translation_error:'Scheduling needs attention',feasible:'Rule check passed',inconclusive:'Rule check inconclusive',configuration_needs_attention:'Configuration needs attention',implementation_error:'Scheduling needs attention',candidate_with_gaps:'Ready for review · gaps remain',unknown_time_limit:result?.validation?'Ran out of time · best proposal found so far':'Ran out of time · no proposal yet',invalid_inputs:'Resolve draft conflicts before generating',unsupported_period:'Planning period exceeds the supported limit',unsupported_size:'Planning scope exceeds the supported limit',unsupported_rules:'Some active rules are not supported'};
      const fits=latest.status==='succeeded'&&!rechecked(latest)?currency(latest,data.current_fingerprint):'',stale=!!fits,discarded=dismissedJobs.has(latest.id)||latest.status==='dismissed';
      const currentDraft=w.status==='draft'&&!w.archived_at,reviewable=currentGeneration(latest,data);
      const submissionIssue=latest.kind==='validation'?(result?.submission_issue??fixSummaryOf(plannerMemo?.page.workspaces.find(r=>r.id===w.id)?.health?.summary)?.submission_issue):null;
      const text=discarded?'Proposal discarded · nothing was applied':fits?(fits==='superseded'?`${SUPERSEDED.state} · review the latest run`:{rota:'Rota changed',scheduler:'Scheduler changed',unknown:'Older run · not confirmed current'}[fits]+' · run auto-schedule again'):latest.status==='succeeded'?(latest.kind==='validation'?(autoChecking&&workspaceId===w.id?'Checking the rota…':submissionIssue?.message||(result?.valid?TERMS.backgroundCheck+' passed':'Must fix found · see the Fix list')):!currentDraft?'This stored result is read-only':result?.status==='candidate_feasible'&&result.validation?.valid===false?`${latest.generation_scope?'This group':'Selected sessions'} checked · whole rota still has mandatory findings`:readyWords(latest,reviewable,data.current_fingerprint)||labels[result?.status]||'Review the result'):latest.status==='applied'?'Assignments added to draft':latest.status==='cancelled'?'Auto-schedule cancelled':'Auto-schedule could not finish';
      const changes=(result?.additions||[]).length+(result?.removals||[]).length;
      sub.textContent=w.name||'';
      const receipt=latest.kind==='generation'&&result?solverReceipt(latest):null;
      if(receipt)feedback.append(receipt);
      const outsideGroup=outsideGroupLine(latest);
      if(outsideGroup)feedback.append(outsideGroup);
      // A result that still applies (not stale, discarded or applied) opens on Review; otherwise What to fill.
      if(!st().step)st().step=reviewable||(latest.status==='succeeded'&&!stale&&!discarded)?3:1;
      staleNote=stale?text:'';
      // F-16: an out-of-date result names what changed since it ran (maintenance's input changes), once read.
      if(fits==='rota'&&!discarded&&!nothingToApply(latest))void get<Row>(`/api/jobs/${encodeURIComponent(latest.id)}/input-changes?offset=0&limit=1`).then(r=>{
        const c=causeOf({changed_facts:r.changes});if(!c||gone()||mine!==loads)return;
        staleNote=`${causeWords(c,'since it ran')} Run auto-schedule again.`;const shown=panel.querySelector('.auto-stale');if(shown)shown.lastChild!.textContent=staleNote;},()=>{});
      // Back to What to fill only when a result you saw current goes out of date here (Undo, an edit); a result
      // already stale when the panel opens keeps the step you choose.
      if(stale&&st().fresh===latest.id){st().fresh='';st().step=1;}else if(!stale&&['succeeded','applied'].includes(latest.status))st().fresh=latest.id;
      // What stopped the run (problems it must keep, or settings), each as an issue card: who, which session, what, fixes.
      const current=currentDraft&&latest.status==='succeeded'&&!stale&&!discarded;
      const blocking:Row[]=!current?[]:(result?.issues||[]).length?result.issues:result?.status==='invalid_inputs'?(result.validation?.findings||[]).filter((f:Row)=>f.severity==='error'):[];
      const infeasible=current&&result?.status==='requirements_conflict';
      // Ran out of time (SR-25): not a failure. It continues from its saved preparation with twice the search time;
      // already at the department maximum it cannot, so it runs again from the start with the time you choose.
      const was=Number(latest.effective_options?.search_minutes)||AUTO_SCHEDULE_LIMITS.default_minutes,doubled=Math.min(was*2,maxMinutes);
      const canContinue=current&&result?.status==='unknown_time_limit'&&editable&&cpSat&&doubled>was;
      const outOfTime=current&&result?.status==='unknown_time_limit'&&!reviewable;
      // Stopped, failed or ended early with a saved checkpoint (result.resume_available): the best found so far can be
      // reviewed and the search continued. A cancelled run keeps none (Cancel discards).
      const resumable=editable&&cpSat&&currentDraft&&!stale&&latest.currency!=='superseded'&&!discarded&&['succeeded','failed'].includes(latest.status)&&!!result?.resume_available
        &&!['needs_more_time','requirements_conflict'].includes(result?.status)&&!['optimal','model_proven'].includes(result?.search_stop?.reason)&&!canContinue;
      if(current&&result?.status==='needs_more_time'){
        feedback.append(outcome('neutral',text,GENERATION_RESUME_NOTE));
        if(editable&&cpSat){const {more,go}=resumeControls(latest,'primary');continuation=go;feedback.append(more.wrap);}
      }else if(outOfTime){
        feedback.append(outcome('neutral',text,'The rota is unchanged.'));
        if(editable&&cpSat&&!canContinue){
          const before=Number(latest.effective_options?.search_minutes)||minutes.value()||Number(defaults.search_minutes);
          const more=timeField('search_minutes',Math.min(maxMinutes,before*2),{max:maxMinutes});
          const said=()=>{const n=more.value();return Number.isFinite(n)?duration(n*60):'…';};
          const again=button(`Run again for ${said()}`,()=>{if(!more.input.reportValidity())return;
            minutes.set(more.value());st().minutes=more.value();return queue('generation');},'primary');
          more.input.addEventListener('input',()=>{again.textContent=`Run again for ${said()}`;});
          continuation=again;feedback.append(more.wrap);
        }
      }else if(infeasible){shortfall=latest;const firstId=(result.summary?.open_places||[])[0]?.shift_id||(result.diagnostics||[]).find((d:Row)=>d.shift_id)?.shift_id,
          first=data.shifts.find(x=>x.id===firstId);
        reasons=shortfallBlock(result,cpSat&&editable?'Fill everything else now; these places stay open for you.':'Nothing was changed.');
        const facts=runFacts(latest),clash=isClash(facts),act=clash&&editable&&cpSat;
        // Nothing more happens until the person chooses (on_conflict 'report', the owner's default). A clash: two choices.
        // Stop and fix the rules (this rota's rules beside the schedule), or Fill everything else (each break a must-fix
        // that blocks publishing). Without a clash the footer offers Fill everything else (shortfall).
        if(act)shortfall=null;
        const choices=act?el('section',{class:'auto-choices','aria-label':'Rules conflict'},el('div',{class:'actions'},
            button('Stop and fix rules',()=>openRotaRules(),'secondary'),button('Fill everything else',()=>fillRest(latest),'primary',{disabled:!runnable(),why:runReason()})),
          el('p',{class:'hint'},icon('alert'),'Every break is a must-fix and blocks publishing until fixed.')):'';
        feedback.append(reasons,clash?clashCard(facts.conflict!,{...clashHooks(data,()=>{}),...(act?{leaveOpen:()=>fillRest(latest)}:{})}):'',choices,
          el('div',{class:'actions'},first?button('Show on schedule',()=>showOnSchedule(first),'secondary small'):'',
            allowed('people')?button('Open People',()=>navigate('people'),'secondary small'):''),
          // A hand-off, not a dead end: who can change eligibility when it is not you (no extra authority is given here).
          !permits(CAPABILITY.CREDENTIALER)&&(result.summary?.top_reasons||[]).some((x:Row)=>ELIGIBILITY_CODES.has(x.code))?el('p',{class:'hint'},'Clinic eligibility and skills are authorised by a credentialer: ask them, or open the person in People to see what is missing.'):'');}
      else if(reviewable){review=latest;feedback.append(proposalSummary(data,latest,{editable,strict:reviewable&&strictlyCurrent(latest,data.current_fingerprint),optimality:candidateOptimality(latest,reviewable&&strictlyCurrent(latest,data.current_fingerprint)),panel:true}));}
      else{
        // Applied: what it did. Stale or discarded: no counts (they describe a rota that no longer exists).
        // The server records what Apply chosen actually applied (result.applied); older jobs applied every suggestion.
        const done=result?.applied,counts=latest.status==='applied'?`${done?.added_count??result?.additions?.length??0} added · ${done?.removed_count??result?.removals?.length??0} removed`
          :!stale&&!blocking.length&&!discarded&&(result?.validation||result?.additions?.length||result?.removals?.length)?`${plural(result.additions?.length||0,'assignment')} to add; ${plural(result.removals?.length||0,'assignment')} to remove${result.validation?` · ${mustWords(result.validation.error_count)}`:` · No candidate validation available`}`:'';
        const tone=latest.status==='applied'||latest.kind==='validation'&&result?.valid&&!submissionIssue?'ok':discarded||stale||latest.status==='cancelled'||!currentDraft?'neutral':latest.kind==='validation'?'warn':'err';
        const none=latest.kind==='generation'&&latest.status==='succeeded'&&!stale&&!discarded&&nothingToApply(latest);
        feedback.append(outcome(none&&String(result?.status).startsWith('candidate_')?'neutral':tone,none?'Nothing to apply':text,none?`The rota is unchanged. ${nothingWhy(result)}`:counts||(tone==='err'&&!blocking.length?(latest.recovery||resumable?'The rota is unchanged.':latest.status==='failed'?'The rota is unchanged. Run it again; if it stops again, open the run details.':String(result?.message||'')):'')));
      }
      if(resumable){const {more,go}=resumeControls(latest,reviewable?'secondary small':'primary');
        // Under a proposal Apply stays the footer's move, so Continue sits here; otherwise Continue is the footer's.
        feedback.append(el('section',{class:'auto-resume','aria-label':'Best found before it stopped'},el('h3',{class:'auto-subhead'},'Best found before it stopped'),
          more.wrap,reviewable?el('div',{class:'actions'},go):''));
        if(!reviewable)continuation=go;}
      if(latest.recovery)feedback.append(recoveryNote(latest.recovery as JobRecovery));
      if(canContinue){const twice=button(`Continue with ${doubled} minutes`,()=>confirmAction('Continue auto-schedule','Continues from the saved inputs with twice the search time. Nothing is applied until you review the proposal.',async reason=>{
          await command<Row>('job.continue',{minutes:doubled,reason},latest.id);await load();},'Continue'),reviewable?'secondary small':'primary');
        if(reviewable)feedback.append(el('div',{class:'actions'},twice));else continuation=twice;}
      feedback.append(
        ...(blocking.length?[...(result.message?[el('p',{class:'muted'},result.message)]:[]),runIssues(data,blocking,'Resolve these first')]:[]),
        // Problems outside the scheduled sessions: left as they are, listed so they are not forgotten.
        ...(current&&(result?.outside_scope_issues||[]).length?[runIssues(data,result.outside_scope_issues,'Outside these sessions · left as they are')]:[]));
      // After a run is applied, the next group is a new run over the current draft and setup (never a Continue of this one).
      const anotherGroup=scopeCap&&editable&&currentDraft&&latest.kind==='generation'&&latest.status==='applied'?button('Schedule another group…',()=>{
        Object.assign(st(),{step:1,group:null,pickGroup:true,groupKeep:undefined,scopeError:''});keptSetup(st());people?.reset(null,true);syncGroup();drawStep();people?.open();},'secondary small'):'';
      feedback.append(el('div',{class:'actions'},anotherGroup,
        current&&submissionIssue&&editable?button('Add session',()=>shiftForm(ctx(),data),'primary small'):'',// SW3-A-07: a rota check is read on the Fix list (its causes, as the footer counts them), not as a per-check table.
        latest.kind==='validation'&&result?button('Open Fix list',()=>openIssues(w.id),'secondary small'):button(result?'Review latest result':'View run details',()=>inspectSchedulingJob(latest),'secondary small'),
        latest.kind==='generation'&&latest.status==='succeeded'&&!stale&&!discarded&&(result?.additions?.length||result?.removals?.length)?button(proposal?.job_id===latest.id?'Hide proposal on grid':'Show proposal on grid',async()=>{
          if(proposal?.job_id===latest.id){proposal=null;}else{proposal={job_id:latest.id,workspace_id:latest.workspace_id,additions:latest.result.additions||[],removals:latest.result.removals||[],job:latest,workspace_revision:w.revision,session:sessionKey()};}
          await renderPage();
        },'secondary small'):''));
    }else{sub.textContent=w.name||'';feedback.append(emptyLine('No auto-schedule runs for this rota yet.'));}
    // Recent runs: one compact row each (what, when, how long, outcome), sized for the side panel; timing details are in Review.
    // A finished run whose rota (or scheduler) has changed since is out of date, not ready to review.
    const fitsNow=(j:Row)=>j.status==='succeeded'?currency(j,data.current_fingerprint):'';
    // A finished generation says what it produced (job.outcome: complete, partial, infeasible, failed, cancelled), not only
    // that the worker finished; applied runs stay "Applied". Checks and older stored jobs have no outcome.
    const runState=(j:Row):[string,Tone|'']=>dismissedJobs.has(j.id)||j.status==='dismissed'?['Discarded','']:j.kind!=='generation'&&j.status==='succeeded'?['Checked','']:j.status==='succeeded'&&nothingToApply(j)?['Nothing to apply','']:fitsNow(j)?[rechecked(j)?'Check again to apply':({unknown:'Not confirmed current',superseded:SUPERSEDED.state} as Partial<Record<Currency,string>>)[fitsNow(j)]||'Out of date','']:j.status!=='applied'&&RUN_OUTCOME[j.outcome]?RUN_OUTCOME[j.outcome]
      :[({succeeded:'Ready for review',applied:'Applied',cancelled:'Cancelled',failed:'Could not finish',queued:'Queued',running:'Running'} as Record<string,string>)[j.status]||j.status,
      ({succeeded:'warn',applied:'ok',failed:'err',queued:'info',running:'info'} as Record<string,Tone>)[j.status]||''];
    const took=(j:Row)=>j.completed_at?` · ${jobSeconds(j.created_at,j.completed_at)}`:'';
    // The latest run is listed too, so the run just applied reads "Applied" beside the older runs it made out of date.
    // Undo is offered while its edit is still the draft's latest (the server undoes the latest draft edit).
    const undoable=(j:Row)=>j.status==='applied'&&editable&&j.result?.applied_content_revision!=null&&j.result.applied_content_revision===w.content_revision;
    // One press of Run is one row: a run that stopped on places nobody can fill and the follow-on that filled the rest
    // (diagnostic_proposal) read as the follow-on alone; the first stays in its Review (G-11).
    const listed=jobs.filter((j,i)=>!(j.kind==='generation'&&j.outcome==='infeasible'&&i>0&&jobs[i-1].kind==='generation'&&jobs[i-1].diagnostic_proposal));
    // The newest applied run (a rota check may be listed above it, maintenance) is the one whose Undo can go.
    const lastApplied=listed.find(j=>j.kind==='generation'&&j.status==='applied')?.id;
    // SW3-A-05: a run applied after the draft on screen (its reply was late or lost) means the page is behind, not that
    // Undo has gone: the page is read again, and only an edit made after the run says "Undo not available".
    const ahead=(j:Row)=>Number(j.result?.applied_content_revision)>Number(w.content_revision);
    const behind=listed.find(j=>j.status==='applied'&&ahead(j)),behindKey=behind?`${w.id}:${behind.result.applied_content_revision}`:'';
    if(editable&&behind&&behindRead!==behindKey){behindRead=behindKey;forgetSchedule();inPlace=false;void refresh();}
    if(listed.length>1||latestRun?.status==='applied')history.append(el('h3',{class:'auto-subhead'},'Recent runs'),el('ul',{class:'run-list'},...listed.slice(0,6).map(j=>{
      const full=j.id===latestRun?.id?latestRun:j,[state,tone]=runState(full),what=j.kind==='generation'?TERMS.autoSchedule:TERMS.backgroundCheck;
      return el('li',{},el('span',{class:'run-what'},el('strong',{},what),el('small',{class:'muted'},stamp(j.created_at)+took(j),...(j.kind==='generation'?[' · ',scopeLine(j.generation_scope)]:[])),
          j.kind==='generation'&&full.status==='succeeded'&&nothingToApply(full)?el('small',{class:'run-why'},nothingWhy(full.result)):''),
        badge(state,tone),
        // The latest applied run whose Undo has gone says why (SR-27): a later edit is the draft's latest now.
        full.status==='applied'&&editable&&full.id===lastApplied&&!undoable(full)&&!ahead(full)?el('small',{class:'muted run-no-undo','data-tip':'The draft changed after this run was applied'},'Undo not available'):'',
        undoable(full)?button('Undo',async()=>{await command('workspace.undo',{reason:'Undo applied auto-schedule suggestions'},w.id,w.revision);await refresh();notify('Undone. The applied suggestions are off the draft.');},'secondary small',{'aria-label':`Undo ${what} of ${stamp(j.created_at)}`}):'',
        // A run still going shows where it is, not Review: Review is for a result reviewed.
        ['queued','running'].includes(j.status)?el('small',{class:'muted run-progress'},j.status==='queued'?'Waiting to start':
          [stageLabel(String((j.id===liveRunning?.id?liveRunning:j).progress?.stage||'checking')),duration(Number((j.id===liveRunning?.id?liveRunning:j).progress?.elapsed_seconds)||Math.max(0,(Date.now()-Date.parse(j.started_at||j.created_at))/1000))].join(' · '))
          :button('Review',()=>inspectSchedulingJob(j),'ghost small',{'aria-label':`Review ${what} of ${stamp(j.created_at)}`}));})));
    jobsKey=nextKey;lastSchedulingBody={id:w.id,feedback:[...feedback.childNodes],history:[...history.childNodes]};
    known=true;drawStep();
    if(keep&&document.activeElement!==had)refocusSpot(feedback,keep);
  };
  await load(initialJobs,firstWoke);  // the jobs just read above: no second request, unless back on the tab since
  // Opening the panel reads again; a failure says so in the panel (failed), not only in a toast over a skeleton.
  return ()=>load().catch(failed);
  }finally{handover();}
}

// Notifications: one item design for the bell menu and the inbox page.
/** Opened or marked read. `read_at` separates reading from acknowledging; older rows only carry `acknowledged_at`. */
function noteRead(n:Row){return !!(n.read_at||n.acknowledged_at);}
/** A notification's kind (the server's `kind`, the event it reports): its icon, colour and the one word shown on the
 *  card and offered by the Kind filter. Weekly availability is a request (kind request). */
const NOTE_KIND:Record<string,[IconName,string,string]>={feedback:['message','teal','Feedback'],publication:['check','violet','Rota published'],review:['shield','indigo','Review'],
  bulk_review:['shield','indigo','Review'],returned:['shield','amber','Returned'],request:['leave','sky','Request'],availability:['leave','amber','Leave or absence'],swap:['swap','pink','Swap'],
  announcement:['bell','teal','Announcement'],self_approval:['shield','slate','Self-approval'],rule:['rules','indigo','Rule'],
  generation_finished:['sparkle','teal',TERMS.autoSchedule]};
const noteKindOf=(n:Row)=>NOTE_KIND[n.kind]||['bell','slate','Other'] as [IconName,string,string];
const ackNote=(n:Row)=>command('notification.ack',{reason:'Explicit acknowledgement by recipient'},n.id,n.revision);
const ackAll=()=>command('notification.ack_all',{reason:'Read'});  // one request for the whole inbox
/** A required announcement is done only when acknowledged, not when opened. */
const acknowledge=(n:Row)=>command('notification.acknowledge',{reason:'Acknowledged by recipient'},n.id,n.revision);
// SW3-C-09: by the department's day, as the Inbox groups it (never "Yesterday" for two days ago, or "20 h ago" yesterday).
function ago(value:string){const m=Math.round((Date.now()-Date.parse(value))/60000),z=zone(),day=toLocal(value,z).slice(0,10),today=toLocal(new Date().toISOString(),z).slice(0,10);
  if(day===today)return m<1?'Just now':m<60?`${m} min ago`:`${Math.round(m/60)} h ago`;return day===addDays(today,-1)?'Yesterday':formatDate(value,z);}
/** Open what a notification is about (and mark it read). */
async function openNote(n:Row){
  if(!noteRead(n))await ackNote(n).catch(()=>undefined);
  openNoteTarget(n);
}
/** Where a notification leads, through the router (and its unsaved-changes guard). An alert's tap opens it this way
 *  too, without marking it read or acknowledging it (build/contracts/PWA-BE.md › destination). */
function openNoteTarget(n:Row){
  const l=n.link||{};
  if(l.staged_id){void openStaged(String(l.staged_id)).catch(e=>{if(!sessionChanged(e))notify((e as Error).message,'error',undefined,e);});void loadNavBadges(true);return;}  // a change sent for independent review
  if(n.kind==='generation_finished'&&l.workspace_id)void rotaMeta(l.workspace_id).then(d=>openPaletteRota(d.workspace,true)).catch(reportError);
  else if(l.feedback_report_id)navigate('feedback?open='+encodeURIComponent(l.feedback_report_id));
  else if(l.publication_id){publicationId=l.publication_id;navigate('published');}else if(l.workspace_id){ctx().setWorkspace(l.workspace_id);navigate('schedule');}
  else if(l.swap_id||l.request_id)navigate('requests?open='+encodeURIComponent(l.swap_id||l.request_id));else if(n.kind==='availability')navigate('requests');
  else if(n.kind==='rule'&&permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER))navigate(ruleNoticeOf(n).hash);else navigate('inbox');
  void loadNavBadges(true);
}
/** A rule notice names its rule (ruleNotice) from the notice's `link.policy_id` and `link.policy_name` (maintenance contract). */
const ruleNoticeOf=(n:Row)=>ruleNotice(n.link?.policy_id,id=>find('policies',id),n.link?.policy_name);
const noteTitle=(n:Row)=>n.kind==='rule'?ruleNoticeOf(n).text:String(n.title);
function noteItem(n:Row,after:()=>void,tag:'li'|'div'='li'){
  const [glyph,tone,label]=noteKindOf(n),title=noteTitle(n),read=noteRead(n),owed=!!n.acknowledgement_required&&!n.acknowledged_at;
  const item=el(tag,{class:'note-item'+(read?' read':'')},el('span',{class:`note-icon duty-${tone}`,'aria-hidden':'true'},icon(glyph)),
    el('button',{type:'button',class:'note-main'},el('span',{class:'note-top'},el('strong',{},title),el('small',{class:'muted'},`${label} · ${ago(n.created_at)}`)),el('span',{class:'note-msg','data-ctip':n.message},n.message)),
    owed?button('I acknowledge',async()=>{await acknowledge(n);after();},'secondary small',{'aria-label':'I acknowledge: '+title,class:'button secondary small note-acknowledge'})
      :read?el('span',{class:'note-read muted','aria-label':n.acknowledged_at&&n.acknowledgement_required?'Acknowledged':'Read'},icon('check')):iconButton('Mark as read: '+title,'check',async()=>{await ackNote(n);after();},{class:'bare note-ack'}));
  item.querySelector('.note-main')!.addEventListener('click',()=>{void openNote(n);});
  // maintenance: a published change says which of your own duties went and came (the message is only a short summary).
  const dc=n.duty_changes as {removed?:Row[];added?:Row[]}|undefined,line=(x:Row,word:string)=>el('li',{},el('span',{class:'muted'},word+' '),
    [formatDay(String(x.start),zone()),`${formatTime(String(x.start),zone())}–${formatTime(String(x.end),zone())}`,x.type,(x.services as string[]|undefined)?.join(', ')||x.location].filter(Boolean).join(' · '));
  if(dc&&(dc.removed?.length||dc.added?.length))item.querySelector('.note-main')!.append(el('ul',{class:'note-duties'},...(dc.removed||[]).map(x=>line(x,'No longer')),...(dc.added||[]).map(x=>line(x,'Now'))));
  // A coordinator's notice about an absence on a published duty: Replace opens on that duty.
  const ra=n.replace_action as {shift_id?:string;assignment_id?:string;start?:string}|undefined;
  if(ra?.shift_id&&ra.assignment_id&&permits(CAPABILITY.COORDINATOR)){const day=ra.start?toLocal(String(ra.start),zone()).slice(0,10):'';
    item.append(button('Replace',()=>navigate(`schedule/week/${day||departmentToday()}?edit=none&shift=${encodeURIComponent(ra.shift_id!)}&replace=${encodeURIComponent(ra.assignment_id!)}`),'secondary small',{'aria-label':'Replace: '+title}));}
  return item;
}
/** The bell's menu opens on the click (maintenance: it used to wait for its read, so a first click looked dead and a second
 *  one closed what the first opened) and fills in when the read lands; a click while open closes it. */
function notificationsMenu(anchor:HTMLElement){
  popover(anchor,(panel,close)=>{panel.classList.add('note-pop');panel.setAttribute('aria-busy','true');
    panel.append(el('div',{class:'note-pop-head'},el('strong',{},TERMS.inbox)),skeleton('lines',`Loading ${TERMS.inbox.toLowerCase()}…`));
    void background<NotificationList>(`/api/notifications?limit=${NOTE_MENU}`).then(list=>{if(panel.isConnected)fill(panel,close,list);},
      e=>{if(!panel.isConnected)return;close();if(!sessionChanged(e))reportError(e,couldntLoad(`the ${TERMS.inbox.toLowerCase()}`)+' ');});
  },{label:TERMS.inbox,placement:'below-end'});
  function fill(panel:HTMLElement,close:()=>void,list:NotificationList){
  // The server's totals, never this page's rows: the header and the bell say the same number (unreadCount, maintenance).
  const messages=list.notifications,total=list.total??messages.length;let left=list.unread_total??0;
  showUnread(left);seenUnread=left;
    const markAll=left?button('Mark all as read',async()=>{await ackAll();close();void loadNavBadges(true);notify('All notifications marked as read.');},'ghost small'):null;
    const count=el('span',{class:'muted'},unreadCount(left).label);
    // SW3-C-10: marking one read (or acknowledging it) keeps the popover open: that item shows read in place, and the
    // count follows, so several are marked without reopening it.
    const item=(n:Row):HTMLElement=>{const li=noteItem(n,()=>{const now=new Date().toISOString();
        const fresh=item({...n,read_at:n.read_at||now,...(n.acknowledgement_required?{acknowledged_at:n.acknowledged_at||now}:{})});
        const back=li.contains(document.activeElement);li.replaceWith(fresh);if(back)fresh.querySelector<HTMLElement>('.note-main')?.focus();
        left=Math.max(0,left-1);count.textContent=unreadCount(left).label;if(!left)markAll?.remove();void loadNavBadges(true);});
      li.addEventListener('click',e=>{if((e.target as HTMLElement).closest('.note-main'))close();});return li;};
    // Focus moves to the first item only if it is still where the open put it (in the panel, or on the bell).
    const here=!document.activeElement||document.activeElement===document.body||document.activeElement===anchor||panel.contains(document.activeElement);
    panel.removeAttribute('aria-busy');
    panel.replaceChildren(el('div',{class:'note-pop-head'},el('strong',{},TERMS.inbox),// An empty inbox says so once (below), not twice.
      messages.length?count:null,el('span',{class:'grow'}),markAll),
      messages.length?el('ul',{class:'note-list'},...messages.map(n=>item(n))):emptyLine('No notifications yet.',null,'note-empty'),
      el('div',{class:'note-pop-foot'},total>messages.length?el('small',{class:'muted'},`Latest ${messages.length} of ${total.toLocaleString('en-GB')}`):null,
        el('span',{class:'grow'}),button('Open inbox',()=>{close();navigate('inbox');},'secondary small')));
    if(here)panel.querySelector<HTMLElement>('.note-main,.note-pop-foot button')?.focus();
  }
}
let inboxSnapshot='',inboxDefaulted='',inboxPulseKey='';
let inboxPending:NotificationList|null=null;
/** How many notifications the bell's menu lists; the inbox page reads the server's page (100) and the next on request. */
const NOTE_MENU=8;
// What the inbox's first page holds, independent of the order equal-time rows come back in: a background check
// (which reads that page) redraws only on a real change, and keeps the pages read with Show more.
const inboxSig=(list:Row[])=>JSON.stringify([...list].sort((a,b)=>String(a.id).localeCompare(String(b.id))));
/** The inbox read: its search and options go to the server (search, search_options), so they span every notification,
 *  not only the latest page; `offset` reads the page after the ones shown. */
const inboxPath=(offset=0)=>{const q=tableSearch('inbox').trim(),so=searchParam(tableSearchOptions('inbox'));
  const query=String(new URLSearchParams({...(q?{search:q}:{}),...(so?{search_options:so}:{}),...(offset?{offset:String(offset)}:{})}));
  return '/api/notifications'+(query?'?'+query:'');};
const inboxRead=newest((path:string)=>get<NotificationList>(path));
async function inboxPage(target:HTMLElement){
  const readPath=inboxPath(),page=inboxPending||await get<NotificationList>(readPath);
  // `totals`: the server's counts for the latest read (maintenance); `messages`: every page read so far.
  const messages:Row[]=[...page.notifications];let totals=page;
  inboxPending=null;inboxSnapshot=inboxSig(page.notifications);
  const unread=page.unread_total??0,{read,...rest}=tableFilters('inbox');
  // Opens on what is unread (once per account); with nothing unread, an Unread-only filter would hide everything.
  if(!unread){if(read?.join()==='Unread')openFiltered('inbox',rest);}
  else if(inboxDefaulted!==me!.identity_id)openFiltered('inbox',{...rest,read:['Unread']});
  inboxDefaulted=me!.identity_id;
  // Announcements are written here too: to everyone, or to roles, ranks, clinics, programmes, tags, skills or named people.
  target.append(sectionHead('Inbox','',permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR)?withIcon(button('New announcement',()=>catalogueForm(ctx(),'department_content'),'primary'),'plus'):null,
    unread?(phoneQuery.matches?iconButton:(label:string,_:IconName,run:()=>void)=>button(label,run,'secondary'))('Mark all as read','check',async()=>{await ackAll();await refresh();notify('All notifications marked as read.');}):null));
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10),yesterday=toLocal(new Date(Date.now()-864e5).toISOString(),zone()).slice(0,10);
  const day=(n:Row)=>{const d=toLocal(n.created_at,zone()).slice(0,10);return d===today?'Today':d===yesterday?'Yesterday':formatDate(d,zone());};
  // maintenance/maintenance: the list says how many of the server's total it shows, and reads the next page on request.
  const shown=el('span',{}),more=el('p',{class:'muted inbox-more'},shown,' ',button('Show more',()=>void loadMore(),'ghost small'));
  const sayMore=()=>{more.hidden=!totals.has_more;shown.textContent=`Showing ${messages.length.toLocaleString('en-GB')} of ${(totals.total??messages.length).toLocaleString('en-GB')} notifications.`;};
  sayMore();
  const loadMore=async()=>{const path=inboxPath(messages.length);
    try{const r=await get<NotificationList>(path);if(path!==inboxPath(messages.length)||!target.isConnected)return;
      const have=new Set(messages.map(n=>n.id));messages.push(...r.notifications.filter(n=>!have.has(n.id)));totals=r;sayMore();table.refresh(true);}
    catch(e){if(!sessionChanged(e)&&target.isConnected)reportError(e,couldntLoad('more notifications')+' ');}};
  // A search reads again from the server and refills this list in place (the box keeps its focus); an older answer is dropped.
  const search=async()=>{const path=inboxPath();
    try{const r=await inboxRead(path);if(!r||path!==inboxPath()||!target.isConnected)return;
      messages.splice(0,messages.length,...r.notifications);totals=r;inboxSnapshot=inboxSig(r.notifications);sayMore();table.refresh(true);}
    catch(e){if(!sessionChanged(e)&&target.isConnected)reportError(e,'Notifications could not be searched. ');return false;}};
  const table=dataTable<Row>({id:'inbox',noun:['notification','notifications'],rows:messages,rowId:n=>n.id,searchLabel:'Search notifications',searchPlaceholder:'Title or message',
    searchIn:'notifications',onSearch:search,serverPaged:true,serverTotal:()=>totals.total??messages.length,  // every page read is drawn; Show more reads the next
    empty:['Your inbox is clear','Rota publications, reviews, requests, swaps and announcements for you appear here.'],
    // Cards grouped by day, newest first.
    defaults:{layout:'cards',group:'day',sort:['day','desc']},
    // The server's counts (unread_total spans the whole inbox, so a search counts the rows it has read).
    views:()=>[{label:'All notifications',filters:{} as Record<string,string[]>,count:totals.total??messages.length},{label:'Unread',filters:{read:['Unread']},count:inboxPath()==='/api/notifications'?totals.unread_total:undefined}],
    columns:[
      {key:'note',label:'Notification',text:n=>`${n.title} ${n.message}`,cell:n=>noteItem(n,()=>void refresh(),'div')},
      {key:'day',label:'Day',hidden:true,ordered:true,facet:day,sort:n=>String(n.created_at),cell:()=>null},
      {key:'kind',label:'Kind',hidden:true,facet:n=>noteKindOf(n)[2],cell:()=>null},
      {key:'read',label:'Read',hidden:true,facet:n=>noteRead(n)?'Read':'Unread',cell:()=>null},
    ]});
  target.append(el('section',{class:'panel inbox-panel'},table.root,more));
  // Opened on Unread (a preset clears the search): rows read for the earlier search are read again for what is shown.
  if(page&&inboxPath()!==readPath)void search();
}

const ROLE_ORDER:readonly string[]=[roleForCapability(CAPABILITY.ADMIN),roleForCapability(CAPABILITY.COORDINATOR),roleForCapability(CAPABILITY.APPROVER),roleForCapability(CAPABILITY.CREDENTIALER),roleForCapability(CAPABILITY.PROGRAMME_LEAD),roleForCapability(CAPABILITY.STAFF),roleForCapability(CAPABILITY.AUDITOR),INSTALLATION_ADMIN_ROLE,roleForCapability(CAPABILITY.OPERATOR)];
/** Access & approvals › People & roles (who signs in, their roles) or › Role history (every grant). */
async function accessPage(target:HTMLElement,data:Promise<Row>,tab:'people'|'history'){
  const access=await data;const identities:Row[]=access.identities;
  const accounts:Record<string,Row>=access.accounts||{};
  const admin=permits(CAPABILITY.ADMIN),fullAccess=admin||may('access.system_roles');
  const invite=admin&&access.accounts_enabled?button('Invite person',()=>inviteDialog(peopleChoices(ctx()),refresh,roleChoices(ROLE_NAMES)),'primary'):null;
  const onboard=admin&&access.accounts_enabled?button('Onboard staff',()=>onboardingPanel(rows('people'),new Set(identities.map(i=>i.person_id).filter(Boolean)),refresh),'secondary'):null;
  target.append(sectionHead('People who can sign in','',invite,onboard,!admin?null:button('Add institutional sign-in',()=>void institutionalSignIn(),access.accounts_enabled?'secondary':'primary',access.accounts_enabled?{}:{'data-tip':'Invitations are off on this server; institutional sign-in still works.'})));
  /** An institutional sign-in is for the one provider this server is set up with (/api/admin/auth-config, the issuer
   *  identity.bind checks against): shown, not typed. Without one there is nothing to bind to: the settings say how. */
  async function institutionalSignIn(){
    const cfg=await get<AuthConfigStatus>('/api/admin/auth-config').catch(e=>{reportError(e);return null;});if(!cfg)return;
    if(!cfg.issuer){showDialog('Institutional sign-in is not set up','',(body,close)=>body.append(note('This server has no institutional sign-in provider yet, so no one can be added this way.','warning'),
      el('div',{class:'actions'},button('Open sign-in settings',()=>{close();navigate('configuration/sign_in');},'primary'))));return;}
    formDialog('Add an institutional sign-in','Type the user ID exactly as your organisation’s identity service gives it. The sign-in has no access until it is given a role.',form=>{
    const issuer=field('Sign-in provider','url',cfg.issuer!,{required:true,readonly:true}),subject=field('User ID (subject)','text','',{required:true}),display=field('Name shown in the app','text','',{required:true}),person=select('Their staff profile',peopleChoices(ctx()),'',false,'None (not on rotas)'),reason=area('Reason');reason.input.required=true;
    form.append(issuer.wrap,...(cfg.institutional_enabled===false?[note('Institutional sign-in is switched off in the sign-in settings: this person can sign in once it is on.','info')]:[]),subject.wrap,display.wrap,person.wrap,reason.wrap);return async()=>{await command('identity.bind',{issuer:cfg.issuer,subject:subject.input.value,display_name:display.input.value,person_id:person.input.value||null,reason:reason.input.value});await refresh();};
  });}
  // Who gives or revokes a role comes from the server permission contract.
  const manages=(r:string)=>!grantRefusal(r,me!.roles);
  // Every role of the contract, in this page's order; one this person may not give is shown disabled with why (maintenance).
  const roleChoices=(roles:readonly string[]):Choice[]=>[...roles].sort((a,b)=>ROLE_ORDER.indexOf(a)-ROLE_ORDER.indexOf(b))
    .map(r=>{const why=grantRefusal(r,me!.roles);return {value:r,label:roleLabel(r),...(why?{disabled:true,hint:why}:{})};});
  // One row per person: how they sign in, which roles they hold now (chips, revocable in place) and whether sign-in is enabled.
  const today=toLocal(new Date().toISOString(),zone()).slice(0,10);
  // The one Give a role form, for one person (their row action) or several (select them, then Give a role).
  const roleForm=(form:HTMLFormElement,roles:Choice[])=>{
    const role=select('Role',roles),start=field('From','date',today,{required:true}),end=field('Until (included, optional)','date'),reason=area('Reason');
    reason.input.required=true;lastDayIncluded(end.input);role.wrap.classList.add('span-2');form.append(role.wrap,dateRange('Dates',start,end),reason.wrap);
    return {role:role.input,from:start.input,until:end.input,reason:reason.input};};
  // A role held from today with no end is not offered again; any other is checked against the chosen dates on save.
  const giveRole=(who:Row)=>formDialog('Give a role to '+who.display_name,'',form=>{
    const held=(r:string)=>access.bindings.filter((b:Row)=>b.identity_id===who.id&&b.role===r);
    const f=roleForm(form,roleChoices(ROLE_NAMES.filter(r=>grantOverlap(held(r),today,null)!=='covered')));
    return async()=>{const clash=grantOverlap(held(f.role.value),f.from.value,f.until.value||null);
      if(clash)throw new Error(clash==='covered'?`${who.display_name} already has the ${roleLabel(f.role.value)} role for these dates.`
        :`${who.display_name} has a ${roleLabel(f.role.value)} grant overlapping these dates. Check Role history and choose a period that does not overlap.`);
      await command('role.grant',{identity_id:who.id,role:f.role.value,starts_on:f.from.value,ends_on:f.until.value||null,reason:f.reason.value});
      notify(`${roleLabel(f.role.value)} role given to ${who.display_name}.`);await refresh();};
  },'Give role');
  const live=(b:Row)=>activeOn(b,today),unended=(b:Row)=>!b.revoked&&(!b.ends_on||b.ends_on>today);
  const grantState=(b:Row)=>b.revoked?'Revoked':live(b)?'Current':b.starts_on>today?'Scheduled':'Ended';
  const rolesOf=(i:Row)=>access.bindings.filter((b:Row)=>b.identity_id===i.id&&unended(b));
  // Removing a role is a row action (never a small × on the chip) that acts at once with Undo in its message; your own
  // asks twice (same-button confirm): without it you may not be allowed to give it back.
  // Your own last Admin role is not offered: removing it would lock you out of this page.
  const removeRoles=(i:Row):RowAction[]=>{const held=rolesOf(i).filter((b:Row)=>manages(b.role)),self=i.id===me!.identity_id;
    return held.filter((b:Row)=>!(self&&b.role===roleForCapability(CAPABILITY.ADMIN)&&held.filter((x:Row)=>x.role===roleForCapability(CAPABILITY.ADMIN)).length<=1)).map((b:Row)=>({label:`Remove ${roleLabel(b.role)} role`,icon:'close' as const,danger:true,armed:self,
      run:async()=>{await command('role.revoke',{reason:'Role removed in People & roles'},b.id,b.revision);notify(`${roleLabel(b.role)} role removed from ${i.display_name}. It ends on their next request.`);await refresh();}}));};
  const enabled=identities.filter(i=>!i.disabled).length;
  // Bulk: never your own identity (self-grants and self-disable are refused anyway).
  const others=identities.filter(i=>i.id!==me!.identity_id);
  const accessActions=(all:string[])=>{const ids=all.filter(id=>id!==me!.identity_id);
    const picked=others.filter(i=>ids.includes(i.id)),held=[...new Set(access.bindings.filter((b:Row)=>ids.includes(b.identity_id)&&unended(b)&&manages(b.role)).map((b:Row)=>String(b.role)))];
    return [
      {label:'Give a role',icon:'plus',run:(i:string[],d:()=>void)=>formDialog('Give a role to '+i.length+' people','',form=>{
        const {role:roleInput,from:start,until:end,reason}=roleForm(form,roleChoices(ROLE_NAMES.filter(r=>r!==roleForCapability(CAPABILITY.ADMIN)))),role={input:roleInput};
        return async()=>{const from=start.value,until=end.value||null;
          if(until&&until<=from)throw new Error('The last included day must be on or after the first day.');
          const targets=others.filter(p=>i.includes(p.id)&&!p.disabled),grant:string[]=[],covered:string[]=[],conflicts:string[]=[];
          if(!targets.length)throw new Error('Select another person with sign-in enabled.');
          for(const p of targets){const clash=grantOverlap(access.bindings.filter((b:Row)=>b.identity_id===p.id&&b.role===role.input.value),from,until);
            if(clash==='covered')covered.push(p.id);else if(clash)conflicts.push(String(p.display_name||'Unnamed account'));else grant.push(p.id);}
          if(conflicts.length)throw new Error(`No roles were granted. These people have grants overlapping those dates: ${conflicts.join(', ')}. Check Role history and choose a non-overlapping period.`);
          const excluded=i.length-targets.length,summary=[covered.length?`${plural(covered.length,'person','people')} already ${covered.length===1?'has':'have'} access for the whole period.`:'',excluded?`${plural(excluded,'selection')} skipped (your own account or disabled sign-in).`:''].filter(Boolean).join(' ');
          if(!grant.length){notify('No changes. '+summary,'info');d();return;}
          await runBulk({collection:'roles',noun:['role','roles'],message:'Role granted to {n} people.'+(summary?' '+summary:''),onDone:async()=>{d();await refresh();},
            body:{item_kind:'role.grant',common:{role:role.input.value,starts_on:from,ends_on:until},items:grant.map(id=>({payload:{identity_id:id}})),reason:reason.value}});};
      },'Give role')},
      ...(held.length?[{label:'Revoke role',icon:'close' as const,run:(i:string[],d:()=>void)=>formDialog('Revoke a role from '+i.length+' people','Takes effect on their next request. The audit log keeps the record.',form=>{
        const role=select('Role to revoke',held.map(r=>({value:String(r),label:roleLabel(String(r))}))),reason=area('Reason');reason.input.required=true;reason.input.minLength=3;form.append(role.wrap,reason.wrap);
        return async()=>{const targets=access.bindings.filter((b:Row)=>ids.includes(b.identity_id)&&i.includes(b.identity_id)&&b.role===role.input.value&&unended(b));
          await runBulk({collection:'roles',noun:['role','roles'],destructive:true,verb:'Revoke',message:'Role revoked for {n} people.',onDone:async()=>{d();await refresh();},
            body:{item_kind:'role.revoke',items:targets.map((b:Row)=>({target_id:b.id,expected_revision:b.revision})),reason:reason.input.value}});};
      },'Revoke role')}]:[]),
      ...(admin&&picked.some(i=>!i.disabled)?[{label:TERMS.disableSignIn,icon:'lock' as const,danger:true,run:(i:string[],d:()=>void)=>confirmAction('Disable sign-in for '+i.length+' people','Their sessions lose access on the next request. You can enable sign-in again later.',async reason=>{
        const targets=others.filter(x=>i.includes(x.id)&&!x.disabled);await runBulk({collection:'identities',noun:['account','accounts'],destructive:true,verb:'Disable sign-in for',message:'Sign-in disabled for {n} people.',onDone:async()=>{d();await refresh();},
          body:{item_kind:'identity.disable',items:targets.map(x=>({target_id:x.id,expected_revision:x.revision})),reason}});},TERMS.disableSignIn)}]:[]),
      ...(admin&&picked.some(i=>i.disabled)?[{label:TERMS.enableSignIn,icon:'check' as const,run:(i:string[],d:()=>void)=>confirmAction('Enable sign-in for '+i.length+' people','They can sign in again with the access roles they still hold.',async reason=>{
        const targets=others.filter(x=>i.includes(x.id)&&x.disabled);await runBulk({collection:'identities',noun:['account','accounts'],message:'Sign-in enabled for {n} people.',onDone:async()=>{d();await refresh();},
          body:{item_kind:'identity.enable',items:targets.map(x=>({target_id:x.id,expected_revision:x.revision})),reason}});},TERMS.enableSignIn)}]:[]),
    ] as BulkAction[];
  };
  const accountState=(i:Row)=>{const a=accounts[i.id];return i.disabled?'Disabled':a&&!a.accepted_at?(a.invite_open?'Invited':a.invite_expires_at?'Invitation expired':'No active invitation'):'Active';};
  const accountActions=(i:Row):RowAction[]=>{const a=accounts[i.id],who=String(i.display_name),acts:RowAction[]=[];
    const link=(kind:string,title:string,said:string)=>async()=>{const r=await command(kind,{reason:said},i.id);await refresh();linkDialog(title,who,r);};
    if(a&&!i.disabled&&!a.accepted_at){
      acts.push({label:a.invite_open?'Resend invitation':'New invitation link',icon:'send',run:link('account.resend','Invitation link','New invitation link')});
      if(a.invite_open)acts.push({label:'Revoke invitation',icon:'close',danger:true,armed:true,run:async()=>{await command('account.revoke_invite',{reason:'Invitation revoked'},i.id);await refresh();notify('Invitation revoked. The link no longer works.');}});}
    if(a&&!i.disabled&&a.accepted_at)acts.push({label:'Password reset link',icon:'lock',run:link('account.reset_link','Password reset link','Password reset link')});
    if(!i.disabled&&(!a||a.accepted_at))acts.push({label:'End all sessions',icon:'logout',danger:true,armed:true,run:async()=>{const r=await command('account.end_sessions',{reason:'Ended all sessions'},i.id);notify(r.ended?`${who} was signed out everywhere (${plural(r.ended,'session')}).`:`${who} had no open sessions.`);}});
    return acts;};
  const people=dataTable<Row>({id:'access',noun:['person','people'],rows:identities,rowId:i=>i.id,searchLabel:'Search people with access',searchPlaceholder:fullAccess?'Name or sign-in':'Name',
    bulk:fullAccess?accessActions:undefined,
    columns:[
      {key:'name',label:'Person',text:i=>String(i.display_name)+' '+(accounts[i.id]?.username||''),cell:i=>el('strong',{},i.display_name)},
      ...(fullAccess?[{key:'issuer',label:'Signs in with',text:(i:Row)=>accounts[i.id]?.username||String(i.issuer||''),facet:(i:Row)=>accounts[i.id]?'Sign-in name':String(i.issuer||''),
        cell:(i:Row)=>accounts[i.id]?el('span',{},el('span',{class:'mono'},accounts[i.id].username),el('small',{class:'muted'},' · '+accounts[i.id].email)):el('small',{class:'mono muted'},i.issuer||'—')}]:[]),
      {key:'roles',label:'Access roles',text:i=>rolesOf(i).map((b:Row)=>roleLabel(b.role)+' '+grantState(b)).join(' '),facet:i=>rolesOf(i).length?rolesOf(i).map((b:Row)=>roleLabel(b.role)):fullAccess?['No current role']:[],sort:i=>rolesOf(i).length,
        cell:i=>el('div',{class:'tag-row'},...rolesOf(i).map((b:Row)=>el('span',{class:'role-chip duty-slate','data-tip':`From ${formatDate(b.starts_on)}${b.ends_on?' until '+formatDate(lastIncludedDate(b.ends_on))+' (included)':' · no end date'}`},roleLabel(b.role),live(b)?null:el('small',{},' · Scheduled from '+formatDate(b.starts_on)))),rolesOf(i).length?null:el('span',{class:'muted'},fullAccess?'No current role':'—'))},
      {key:'status',label:'Status',facet:i=>accountState(i),cell:i=>{const state=accountState(i),a=accounts[i.id];
        return el('span',{class:'status-dot '+(state==='Active'?'on':'off'),'data-tip':a&&!a.accepted_at&&a.invite_expires_at?(a.invite_open?'Link expires ':'Link expired ')+formatDate(a.invite_expires_at,zone()):''},state);}},
      ...(fullAccess?[{key:'last',label:'Last sign-in',text:(i:Row)=>String(i.last_sign_in_at||''),sort:(i:Row)=>String(i.last_sign_in_at||''),cell:(i:Row)=>i.last_sign_in_at?el('span',{},dateUnits([formatDate(i.last_sign_in_at,zone())]),el('small',{class:'muted'},' '+formatTime(i.last_sign_in_at,zone()))):el('span',{class:'muted'},admin||may('access.system_roles')?'Never':'—')}]:[]),
    ],
    rowActions:i=>i.id===me!.identity_id?removeRoles(i):!admin?(i.disabled?removeRoles(i):[{label:'Give a role',icon:'plus' as const,run:()=>giveRole(i)},...removeRoles(i)]):[...(!i.disabled?[{label:'Give a role',icon:'plus' as const,run:()=>giveRole(i)}]:[]),...removeRoles(i),...accountActions(i),...(!i.disabled?[{label:TERMS.disableSignIn,icon:'lock' as const,danger:true,run:()=>confirmAction(TERMS.disableSignIn,'Existing sessions lose access on their next request. You can enable sign-in again later. Cross-department sign-ins need the institutional operator.',async reason=>{await command('identity.disable',{reason},i.id,i.revision);await refresh();},TERMS.disableSignIn)}]
      :[{label:TERMS.enableSignIn,icon:'check' as const,run:()=>confirmAction(TERMS.enableSignIn,'They can sign in again with the access roles they still hold.',async reason=>{await command('identity.enable',{reason},i.id,i.revision);await refresh();},TERMS.enableSignIn)}])]});
  // Every grant, current and past, as its own tab (effective dates are checked on every request).
  const who=(b:Row)=>String(identities.find(i=>i.id===b.identity_id)?.display_name||'Institutional identity');
  const history=()=>dataTable<Row>({id:'access-history',noun:['grant','grants'],rows:access.bindings,rowId:b=>b.id,searchLabel:'Search access history',searchPlaceholder:'Person or role',defaults:{group:'person'},
    columns:[
      {key:'role',label:'Role',text:b=>roleLabel(b.role),facet:b=>roleLabel(b.role),cell:b=>el('span',{class:'role-chip duty-slate'},roleLabel(b.role))},
      {key:'person',label:'Person',text:who,facet:who,cell:who},
      {key:'from',label:'From',sort:b=>String(b.starts_on),cell:b=>dateUnits([formatDate(b.starts_on)])},
      {key:'until',label:'Until (included)',sort:b=>String(b.ends_on||'9999'),cell:b=>b.ends_on?dateUnits([formatDate(lastIncludedDate(b.ends_on))]):'No end date'},
      {key:'status',label:'State',facet:grantState,cell:b=>statusBadge(live(b)?'current':'',grantState(b))}]}).root;
  // Access & approvals › People & roles or › Role history (page tabs, PAGE_TABS).
  if(tab==='history'){target.replaceChildren(el('section',{class:'panel'},history()));return;}
  target.append(el('section',{class:'panel'},people.root));
}

let operationsOffset=0;
// Search and every selected state/kind span the full department outbox, before the server counts and pages it.
const outboxQuery=()=>{const f=tableFilters('outbox'),p=new URLSearchParams({offset:String(operationsOffset)}),q=tableSearch('outbox').trim();
  if(f.state?.length)p.set('state',f.state.map(s=>s.replaceAll(' ','_')).join(','));if(f.kind?.length)p.set('kind',f.kind.join(','));if(q)p.set('q',q);
  // Or / is-not conditions are the server's too (before it pages), in its canonical states.
  // The search's options too (filter.search_options): one predicate for the total and the page.
  const conditions=tableServerConditions('outbox'),so=tableSearchOptions('outbox');
  if(conditions||so)p.set('filter',JSON.stringify({...(conditions?{conditions}:{}),...(so?{search_options:so}:{}),...(conditions&&tableAsOf('outbox')?{as_of:tableAsOf('outbox')}:{})}));
  return '/api/operations?'+p.toString();};
// Operations follows the worker only while it has work (a queued or running job, a notification waiting to go out);
// an idle page waits for the pulse instead of re-reading every 10 seconds.
let opsBusy=false;
/** SW3-D-24: a failed or cancelled job count opens those jobs (newest first, the server's status filter), each with its
 *  run details; the count alone could not be inspected. */
/** Who may open a run's details, as GET /api/jobs/{id} checks (main.py job_detail): a coordinator, and an approver
 *  for a feasibility or catalogue-proof run. Never offered to a reader the server refuses (operators, auditors). */
const mayReadJob=(j:Row)=>permits(CAPABILITY.COORDINATOR)||['feasibility','catalogue_proof'].includes(String(j.kind))&&permits(CAPABILITY.APPROVER);
function jobsInState(state:string,count:number):HTMLElement{
  const b:HTMLButtonElement=el('button',{type:'button',class:'ops-state err ops-open',...popup('dialog')},el('b',{},String(count)),state.replaceAll('_',' ')) as HTMLButtonElement;
  b.addEventListener('click',()=>popover(b,panel=>{const list=el('ul',{class:'ph-rows'},el('li',{},skeleton('line',`Loading ${state.replaceAll('_',' ')} jobs…`)));
    panel.append(el('div',{class:'popover-head'},el('strong',{},`${count} ${state.replaceAll('_',' ')}`)),el('div',{class:'popover-section'},list));
    get<{jobs:Row[];has_more:boolean}>(`/api/jobs?status=${encodeURIComponent(state)}&limit=20`).then(r=>{list.replaceChildren(...r.jobs.map(j=>el('li',{},
        el('span',{},[String(j.kind).replaceAll('_',' ').replace(/^./,c=>c.toUpperCase()),j.error_code?String(j.error_code):'',
          `${formatDate(String(j.created_at),zone())} ${formatTime(String(j.created_at),zone())}`].filter(Boolean).join(' · ')),' ',
        mayReadJob(j)?button('Run details',()=>void inspectSchedulingJob(j).catch(reportError),'link-button'):'')),
        ...(r.has_more?[el('li',{class:'muted'},`Newest ${r.jobs.length} shown`)]:[]));},
      e=>{if(!sessionChanged(e))list.replaceChildren(el('li',{},note((e as Error).message,'error',e)));});},{label:`${state} jobs`,placement:'below'}));
  return b;}
async function operationsPage(target:HTMLElement){
  const data=await get(outboxQuery()),m=data.metrics;
  opsBusy=m.job_states.queued+m.job_states.running+m.outbox_states.queued+m.outbox_states.retry>0;
  // Health at a glance: tiles coloured by state, one queue grid, and the measurement notes on demand.
  const age=(v:number|null)=>v===null?'None':duration(v);
  const tile=(label:string,value:string,tone:'ok'|'warn'|'err'|'na',sub='')=>el('div',{class:'ov-stat ops-tile '+tone},el('span',{class:'ov-stat-label'},el('span',{class:'ops-dot','aria-hidden':'true'}),label),el('strong',{class:/^[\d.]+( \w+)?$/.test(value)?'':'text'},value.replace(/^./,c=>c.toUpperCase())),sub?el('small',{},sub):null);
  target.append(el('div',{class:'ov-stats ops-stats'},
    tile('Service',me?.deployment==='production'?'Production':'Test','ok',`Database ${data.database_backend} · notifications ${String(data.notification_channel).replaceAll('_',' ')}`),
    tile('Worker',({running:'Running',not_responding:'Not responding',never_seen:'Never started'} as Record<string,string>)[m.worker_liveness]||String(m.worker_liveness),m.worker_liveness==='running'?'ok':'warn',m.worker_liveness==='running'?plural(m.workers_running,'worker')+' checked in within a minute':m.worker_last_seen_age_seconds!=null?`Last seen ${Math.round(m.worker_last_seen_age_seconds/60)} min ago — notifications paused`:'No worker yet — notifications paused'),
    tile('Notifications due',String(m.outbox_due_count),m.outbox_due_count?'warn':'ok',m.outbox_oldest_due_age_seconds===null?'Nothing waiting':`Oldest waiting ${age(m.outbox_oldest_due_age_seconds)}`),
    tile('Background jobs',m.oldest_queued_job_age_seconds===null?'Nothing queued':age(m.oldest_queued_job_age_seconds),m.expired_running_job_leases||m.running_jobs_without_lease_start?'err':'ok',m.expired_running_job_leases||m.running_jobs_without_lease_start?`${m.expired_running_job_leases} expired · ${m.running_jobs_without_lease_start} not started`:'No stuck jobs'),
    tile('Imports',m.latest_import_received_age_seconds===null?'No imports yet':age(m.latest_import_received_age_seconds)+' ago',m.source_completeness==='unqualified'?'na':'ok',m.source_completeness==='unqualified'?'Source not yet verified':`Source ${String(m.source_completeness).replaceAll('_',' ')}`)));
  const queues:[string,string][]=[['outbox_states','Notifications'],['job_states','Scheduling jobs'],['import_states','Imports']];
  const bad=new Set(['failed','retry','cancelled']);
  const measurements:HTMLButtonElement=iconButton('How these are measured','help',()=>popover(measurements,panel=>panel.append(
    el('div',{class:'popover-section'},el('p',{},'Counts are read from the database when the page loads; each queue is read separately, so they can differ by a few seconds.'))),{label:'How these are measured'}),{class:'bare'});
  // Only the states that hold something; a queue with nothing in it says so once.
  target.append(el('section',{class:'panel'},sectionHead('Queue observations',`Observed ${formatDate(m.observed_at,zone())} ${formatTime(m.observed_at,zone())}`,measurements),
    el('div',{class:'ops-queues'},...queues.map(([key,label])=>{const held=Object.entries(m[key] as Record<string,number>).filter(([,count])=>count>0);
      return el('div',{class:'ops-queue'},el('h3',{},label),el('div',{class:'ops-states'},...(held.length?held.map(([state,count])=>
        key==='job_states'&&bad.has(state)&&permits(CAPABILITY.OPERATOR,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)?jobsInState(state,count)
          :el('span',{class:'ops-state'+(bad.has(state)?' err':' busy')},el('b',{},String(count)),state.replaceAll('_',' '))):[el('span',{class:'muted'},'Nothing queued')])));}))));
  const rows:Row[]=[...data.outbox],kinds:string[]=[...data.kinds],readStatus=el('div');
  // The toolbar counts the events; only a list of several server pages says which part is shown.
  const say=(r:Row)=>r.has_more||r.offset?`${Math.min(r.offset+1,r.total)}–${Math.min(r.offset+r.outbox.length,r.total)} of ${r.total}`:'';
  // Refill the same table in place, so an open filter menu stays attached while the server pages.
  const read=newest((path:string)=>get(path));
  const load=async()=>{const path=outboxQuery();readStatus.replaceChildren();
    try{const r=await read(path);if(!r||path!==outboxQuery()||!target.isConnected)return;
      rows.splice(0,rows.length,...r.outbox);kinds.splice(0,kinds.length,...r.kinds);paint(r);outbox.refresh(true);}
    catch(e){if(path===outboxQuery()&&target.isConnected&&!sessionChanged(e))readStatus.replaceChildren(
      readFailed('events',e,()=>void load(),'events','The list has not been updated.'));return false;}};
  // Returns the read (false once a failure is reported), so a failed search clears the list's busy rows.
  const refilter=()=>{operationsOffset=0;return load();};
  const prev=iconButton('Previous events','chevronLeft',()=>{operationsOffset=Math.max(0,operationsOffset-data.limit);return load();}),next=iconButton('Next events','chevronRight',()=>{operationsOffset+=data.limit;return load();});
  const where=el('span',{class:'muted'},say(data)),head=sectionHead('Notification outbox','',prev,where,next);
  let total=Number(data.total)||0;
  const paint=(r:Row)=>{total=Number(r.total)||0;where.textContent=say(r);prev.disabled=r.offset===0;next.disabled=!r.has_more;prev.hidden=next.hidden=!r.has_more&&r.offset===0;};
  // SW3-D-24: the count is the server's total ("148 events" beside "1–100 of 148"), not the page's 100.
  const outbox=dataTable<Row>({id:'outbox',noun:['event','events'],rows,rowId:e=>e.id,serverPaged:true,serverTotal:()=>total,searchLabel:'Search events',searchPlaceholder:'Event, state or error',
      empty:['No notifications waiting',''],onFilter:refilter,onSearch:refilter,searchIn:'operations',
      columns:[
        // SW3-D-24: the event in words ("Absence approved", not "Absence_approved"); filter values stay the server's.
        {key:'kind',label:'Event',server:{field:'kind'},text:e=>String(e.kind),facet:e=>String(e.kind),options:kinds,cell:e=>el('strong',{},String(e.kind).replaceAll('_',' ').replace(/^./,(c:string)=>c.toUpperCase()))},
        {key:'state',label:'State',server:{field:'state',value:v=>v.replaceAll(' ','_')},facet:e=>String(e.state).replaceAll('_',' '),options:['queued','retry','failed','available in app'],cell:e=>status(e.state)},
        {key:'attempts',label:'Attempts',num:true,sort:e=>Number(e.attempts),cell:e=>e.attempts},
        {key:'error',label:'Last error',text:e=>String(e.last_error||''),cell:e=>e.last_error||el('span',{class:'muted'},'—')},
      ],
      rowButtons:e=>e.state==='failed'&&permits(CAPABILITY.OPERATOR)?[button('Retry',()=>confirmAction('Retry notification event','Try sending this notification again. People will not receive duplicate inbox items.',async reason=>{await command('outbox.retry',{reason},e.id,e.revision);await refresh();},'Confirm',undefined,true),'secondary small')]:[]});
  paint(data);target.append(el('section',{class:'panel'},head,outbox.root,readStatus),
    // Service health in the footer: the worker and what is due, the two things an operator acts on.
    footerStatus(statusBadge(m.worker_liveness==='running'?'current':'attention',m.worker_liveness==='running'?'Worker running':'Worker not running'),el('span',{class:'muted'},m.outbox_due_count?plural(m.outbox_due_count,'notification')+' due':'Nothing due')));
}
let auditOffset=0;
// The audit log reads as sentences: who did what, grouped by day, with runs of the same change folded together.
// Coral reads as a problem: sign-ins, devices and roles are ordinary records here (P-04), so neutral.
const AUDIT_NOUN:Record<string,[string,string,string]>={review_route:['review route','review routes','amber'],workspace:['rota','rotas','violet'],shift:['session','sessions','teal'],catalogue:['setting','settings','amber'],policy:['rule','rules','indigo'],
  exception:['rule exception','rule exceptions','indigo'],role:['role','roles','slate'],identity:['sign-in identity','sign-in identities','slate'],department:['department setting','department settings','amber'],
  availability:['absence or leave record','absence or leave records','blue'],availability_pattern:['weekly availability','weekly availability','blue'],wish:['wish','wishes','blue'],table_view:['saved view','saved views','slate'],request:['request','requests','blue'],swap:['swap','swaps','pink'],membership:['rank','ranks','amber'],eligibility:['eligibility','eligibilities','amber'],
  grant:['capability','capabilities','amber'],closure:['closure','closures','amber'],publication:['publication','publications','violet'],scheduling:['auto-schedule run','auto-schedule runs','violet'],
  job:['check','checks','violet'],generation:['auto-schedule result','auto-schedule results','violet'],series:['recurring session','recurring sessions','teal'],
  reference:['import','imports','amber'],notification:['notification','notifications','slate'],calendar_feed:['calendar link','calendar links','slate'],session:['device','devices','slate'],auth:['device','devices','slate'],self_approval:['self-approval permission','self-approval permissions','slate'],tag:['tag','tags','amber'],
  // Not in the contract's catalogue (command_refusals.py writes it): the words a refused change reads with.
  command:['change','refused changes','slate']};
const AUDIT_VERB:Record<string,string>={change:'changed',withdraw_approval:'withdrew an approval to retire or restore',retire_pending:'approved a pending retirement of',restore_pending:'approved a pending restoration of',restore:'restored',members:'changed the members of',member_add:'added a member to',member_remove:'removed a member from',create:'added',update:'changed',publish:'published',approve:'approved',submit:'submitted',validate:'checked',reject:'returned',retire:'retired',activate:'activated',
  propose:'proposed',revoke:'revoked',grant:'granted',disable:'disabled',bind:'bound',end:'ended',record:'recorded',cancel:'closed',decide:'decided',action:'updated',settings:'updated',display:'updated',offer:'offered',counteroffer:'offered a duty in return for',confirm:'confirmed',withdraw:'withdrew',prepare:'prepared a rota change for',rule_adjust:'changed a rule for',decline:'declined',bulk_update:'changed',delete:'deleted',undo:'undid a change in',apply:'applied',link_rota:'linked a rota to',ack:'read',ack_all:'read all',activate_many:'activated',expand:'added sessions from',reconcile:'updated',sign_in:'signed in on',sign_out:'signed out on',session_revoke:'ended',refused:'was refused'};
/** One wording for a history entry (overview and audit log): chip, verb and what, counting folded repeats. The contract's
 *  AUDIT_ACTION_CATALOGUE says each action first; AUDIT_NOUN / AUDIT_VERB cover only actions it lacks. A raw action name
 *  never shows (maintenance, the maintenance rule): an unknown one reads "Other · changed a record" (tests/test_web_copy.py). */
function auditWords(action:string,n=1,e?:Row){
  const [head,...rest]=String(action).split('.');
  // Sign-in events read as what the person did ("signed in"), never "signed in on auth".
  // A configuration change names its list ("location type", not "setting") and, when it still exists, the record;
  // its chip stays the Settings group.
  const list=head==='catalogue'&&e?.detail?.catalogue as string|undefined,known=list&&singularOf(list)!=='record';
  const cat=(AUDIT_ACTION_CATALOGUE as Record<string,{noun:string;plural:string;colour:string;verb:string}>)[action];
  const noun:[string,string,string]|undefined=cat?[cat.noun,cat.plural,cat.colour]:AUDIT_NOUN[head],said:[string,string]|undefined=known?[singularOf(list),(kindLabel(list)||list).toLowerCase()]:noun&&[noun[0],noun[1]];
  let verb=cat?.verb||AUDIT_VERB[rest.at(-1)||'']||'changed';if(head==='job'&&verb==='added')verb='started';
  // Folded sign-ins count events, not devices (one browser signs in many times): "signed in 4 times".
  if(n>1&&head==='auth'&&/^signed (in|out) on$/.test(verb))return {noun,head,verb:verb.slice(0,-3),what:`${n} times`,chip:noun![1][0].toUpperCase()+noun![1].slice(1),colour:noun![2]};
  // Records without a name of their own (a rotation, an attachment) read by their kind alone.
  // SW3-D-22: the server's subject (event-time: whose role, which permission) first, else the record by its id.
  const named=known&&n===1&&e?.target_id?find(list,e.target_id):undefined,title=(n===1?e?.target_label:'')||named?.name||named?.display_name;
  const what=(said?(said[0]===said[1]?said[0]:n>1?`${n} ${said[1]}`:`${/^[aeiou]/i.test(said[0])?'an':'a'} ${said[0]}`):'a record')+(title?` “${title}”`:'');
  return {noun,head,verb,what,chip:noun?noun[1][0].toUpperCase()+noun[1].slice(1):'Other',colour:noun?.[2]||'slate'};
}
const AUDIT_GROUPS:[string,string,string[]][]=[['','All',[]],['rotas','Rotas & sessions',['workspace','shift','publication','scheduling','job','generation','series']],['config','Configuration',['catalogue','department','membership','eligibility','grant','closure','tag']],
  ['rules','Rules',['policy','exception']],['people','Requests & swaps',['availability','availability_pattern','request','swap']],['access','Access',['role','identity','self_approval']],['self','Self-approved',[]]];
let auditFilter='',auditSearch='',auditFrom='',auditTo='',auditPerson='';
/** Audit search options (search.ts), sent as filter.search_options to the page and its CSV alike. */
let auditOptions:SearchOptions|undefined;
/** The page's filters as server query parameters, so they span all history rather than one loaded page. */
function auditQuery(){
  const p=new URLSearchParams(),group=AUDIT_GROUPS.find(g=>g[0]===auditFilter)!;
  if(auditFilter==='self')p.set('self_approved','true');else for(const a of group[2])p.append('action',a);
  for(const [k,v] of [['from',auditFrom],['to',auditTo],['actor_id',auditPerson],['q',auditSearch.trim()]])if(v)p.set(k,v);
  const so=normalSearch(auditOptions);if(so)p.set('filter',JSON.stringify({search_options:so}));
  return p.toString();
}
/** History rows by day (System › Audit and a person's History): consecutive events with the same person, change and
 *  reason fold into one. */
function auditFeed(events:Row[]):DocumentFragment{
  const out=document.createDocumentFragment(),selfApproved=(e:Row)=>!!e.detail?.self_approved;
  const runs:{e:Row;n:number;last:Row}[]=[];
  for(const e of events){const r=runs.at(-1);if(r&&r.e.action===e.action&&!e.detail?.leave_allowance_changes&&!r.e.detail?.leave_allowance_changes&&r.e.detail?.catalogue===e.detail?.catalogue&&(r.e.target_label||'')===(e.target_label||'')&&r.e.actor_id===e.actor_id&&r.e.reason===e.reason&&toLocal(r.e.created_at,zone()).slice(0,10)===toLocal(e.created_at,zone()).slice(0,10)){r.n++;r.last=e;}else runs.push({e,n:1,last:e});}
  let day='';
  for(const {e,n,last} of runs){
    const d=toLocal(e.created_at,zone()).slice(0,10);
    if(d!==day){day=d;out.append(el('h3',{class:'audit-day'},new Intl.DateTimeFormat('en-GB',{weekday:'long',day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'))));}
    const {verb,what,chip,colour}=auditWords(e.action,n,e);
    const time=n>1&&formatTime(last.created_at,zone())!==formatTime(e.created_at,zone())?`${formatTime(last.created_at,zone())}–${formatTime(e.created_at,zone())}`:formatTime(e.created_at,zone());
    // The reference (account, correlation and action) is for support: one Copy reference button, not read out per row.
    const ref=`Account ${e.actor_id} · Reference ${e.correlation_id} · ${e.action}`;
    const copyRef=iconButton(`Copy reference for ${time} change`,'copy',async()=>{try{await navigator.clipboard.writeText(ref);notify('Reference copied.');}catch{notify('Copying is blocked here. The reference is '+ref,'info');}},{class:'audit-ref','data-tip':ref});
    // A reason that only repeats the change ("Signed in" on a sign-in) is left out.
    const said=e.reason&&e.reason!==e.action&&!verb.startsWith(String(e.reason).toLowerCase())?e.reason:'';
    const allowances=(e.detail?.leave_allowance_changes||[]) as {leave_type_name:string|null;effective_year_start:string|null;before_days:number|null;after_days:number|null}[];
    const days=(value:number|null)=>value===null?'No allowance':plural(value,'calendar day');
    out.append(el('div',{class:'audit-row'},el('span',{class:'audit-time mono'},time),el('span',{class:`role-chip duty-${colour}`,title:chip},el('span',{class:'chip-text'},chip)),
      el('div',{class:'audit-text'},el('span',{},el('strong',{},e.actor_display_name||'Account not available'),` ${verb} ${what}${verb==='submitted'?' for review':''}`,selfApproved(e)?badge('Self-approved','warn','audit-self',{tip:'Their own work, allowed by a self-approval permission'}):''),said?el('small',{title:said},'“'+said+'”'):null,
        allowances.map(line=>el('span',{},`${line.leave_type_name||'Leave'}${line.effective_year_start?' from '+formatDate(line.effective_year_start):''} · ${days(line.before_days)} → ${days(line.after_days)}`))),copyRef));
  }
  return out;
}
async function auditPage(target:HTMLElement){
  let data=await get(`/api/audit?offset=${auditOffset}&${auditQuery()}`);
  const list=el('div',{class:'audit-feed'}),pager=el('div',{class:'dt-pager'}),shown=el('span',{class:'muted'});
  const failed=(e:unknown)=>notify((e as Error).message,'error',undefined,e);
  const read=newest((path:string)=>get(path));
  // The people list (for the Person filter) is read once per visit; reloads skip it.
  const reload=async(first=true)=>{if(first)auditOffset=0;const next=await read(`/api/audit?offset=${auditOffset}&people=false&${auditQuery()}`);if(next){data=next;draw();}};
  const people=[{value:'',label:'Everyone'},...(data.people as Row[]).map(p=>({value:p.id,label:p.display_name||'Account not available'})).sort(byLabel)];
  const exportCsv=button('Export CSV',()=>download('/api/audit.csv?'+auditQuery()),'secondary small',{'data-tip':'Download every change matching these filters'});
  // Filters go to the server, so they span all history rather than one loaded page of 100.
  const bar=collectionToolbar({label:'Audit log',
    search:{value:auditSearch,label:'Search history',placeholder:'Reason or person',wait:300,onInput:q=>{auditSearch=q;},apply:()=>void reload().catch(failed),
      options:{value:()=>auditOptions,set:o=>{auditOptions=o;},can:serverCapability('audit')!}},
    filter:{count:()=>[auditFilter,auditPerson,auditFrom,auditTo].filter(Boolean).length,clear:()=>{auditFilter=auditPerson=auditFrom=auditTo='';void reload().catch(failed);},
      build:(panel,changed)=>{
        const kind=select('Kind of change',AUDIT_GROUPS.map(([value,label])=>({value,label})),auditFilter,false),person=select('Person',people,auditPerson,false);
        const from=field('From date','date',auditFrom),to=field('To date','date',auditTo);
        for(const [f,set] of [[kind,(v:string)=>{auditFilter=v;}],[person,(v:string)=>{auditPerson=v;}],[from,(v:string)=>{auditFrom=v;}],[to,(v:string)=>{auditTo=v;}]] as [{input:HTMLInputElement|HTMLSelectElement},(v:string)=>void][])
          f.input.addEventListener('change',()=>{set(f.input.value);changed();void reload().catch(failed);});
        panel.append(el('div',{class:'dt-pop-fields'},kind.wrap,person.wrap,dateRange('Dates',from,to)));}},
    actions:[el('span',{class:'grow'}),exportCsv]});
  const draw=()=>{
    const events:Row[]=data.events,runs=events.length;
    list.replaceChildren(auditFeed(events));
    if(!runs)list.append(auditQuery()||auditOffset?empty('Nothing matches','Try another kind of change, other dates or clear the search.',null,'search'):empty('No decisions yet','Every change, approval and publication is recorded here.',null,'history'));
    shown.textContent=events.length?`${auditOffset+1}–${auditOffset+events.length} changes`:'No changes';bar.shown(runs,undefined,['change','changes']);
    pager.replaceChildren(...(auditOffset||events.length>=100?[iconButton('Previous page','chevronLeft',()=>{auditOffset=Math.max(0,auditOffset-100);return reload(false);},{disabled:auditOffset===0}),
      el('span',{class:'muted'},`Page ${auditOffset/100+1}`),iconButton('Next page','chevronRight',()=>{auditOffset+=100;return reload(false);},{disabled:events.length<100})]:[]));
    bar.sync();
  };
  target.append(el('section',{class:'panel'},bar.root,list,pager),footerStatus(shown));draw();
}

/** Home's Self-approved decisions: the last 30 days the server lets this reader see (the department's, or their own).
 *  A list, so it docks beside the page (DESIGN: lists are panels, not modals). */
function selfApprovedPanel(){
  dockPanel({key:'self-approved',title:'Self-approved decisions',build:({body})=>{
    const load=()=>get(`/api/audit?self_approved=true&people=false&from=${addDays(departmentToday(),-30)}`).then(d=>{
      body.replaceChildren(d.events.length?auditFeed(d.events):empty('No self-approved decisions','None in the last 30 days.',null,'history'));
    },e=>{if(!sessionChanged(e))body.replaceChildren(failedNote(couldntLoad('the self-approved decisions',e),e,load));});
    void load();}});
}

let polling=false;
window.addEventListener('session-expired',e=>{const code=(e as CustomEvent).detail?.code;
  // Revoked access starts over; an ended session keeps the page for the same account (maintenance).
  if(me&&!signingOutNow())void showLogin(SIGNED_OUT[code]||'Your session ended. Sign in to continue where you left off.',code==='ACCESS_REVOKED'?'':accountKey(me));});
// The skip link moves focus to the page; it is not an address (N-02).
document.addEventListener('click',e=>{const a=(e.target as Element|null)?.closest?.('a.skip-link');if(!a)return;e.preventDefault();document.querySelector<HTMLElement>('#main')?.focus();});
window.addEventListener('hashchange',()=>{if(location.hash==='#main'){history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;document.querySelector<HTMLElement>('#main')?.focus();return;}
  // Back on the entry of the page shown: the step back over a refused Back (below) arriving.
  const at=stepOf();if(at===shownStep){routeSeen=location.hash;return;}
  // Refused: step back over the entry Back or Forward reached (its place is known). An entry without a place (from an
  // earlier release, a link, a typed address) may lie behind or ahead: rewrite it to this page rather than walk on,
  // which could reach a further entry and discard the unsaved changes unasked.
  // Discard in the leave dialog goes on to what was asked: the same history entry again, or the link's address.
  if(me){const wanted=location.hash;
    if(!leaving(()=>{if(at===null)location.hash=wanted;else history.go(at-shownStep);},pageName(wanted))){if(at===null){history.replaceState(null,'',routeHash(view,routeParts,routeQuery));routeSeen=location.hash;}else history.go(shownStep-at);return;}
  // A link or a typed address added an entry of its own: it comes next.
  // Going to another page (Back, a link) closes windows left open on the previous one, before its address is read:
  // a window that put the page back as it was (Print or export) does so now, never over the next page.
  closeDialogs();
  if(at===null)history.replaceState({step:++shownStep},'');else shownStep=at;readRoute('home');void openPage();}});
// A new frontend was deployed while this tab stayed open: offer a reload (never forced, forms stay intact).
let seenBuild='';
function noticeNewVersion(build:string|null|undefined){
  if(!build)return;noticeServerBuild(build);if(!seenBuild){seenBuild=build;return;}
  if(build===seenBuild)return;
  updateWorker();newVersion(()=>{seenBuild=build;});
}
/** The one "new version" notice (N-18), from a deploy seen by the live pulse or a page older than the server at start:
 *  plain words, Reload or Later; the loss of unsaved entries is mentioned only when there are some. Release numbers
 *  are in About Rota. */
/** Entries not yet saved on screen: a form's unsaved-changes guard, a docked panel, staged sheet cells. */
const unsavedNow=()=>!!leaveCheck?.()||dockDirty()||sheetStagedCount()>0;
// maintenance: any failure in an outdated tab (stale_bundle.ts updateAfter) keeps the dirty-close rule: unsaved or a dialog open, the update bar.
staleHooks({unsaved:()=>unsavedNow()||!!document.querySelector('dialog[open]'),ask:()=>newVersion()});
function newVersion(later=()=>{}){
  if(document.querySelector('.update-bar'))return;
  const unsaved=unsavedNow();
  const reload=button('Reload',reloadToUpdate,'primary small'),dismiss=button('Later',()=>{later();bar.remove();},'ghost small');
  const bar=el('div',{class:'update-bar',role:'status'},icon('sparkle'),el('span',{},el('strong',{},`A new version of ${brand().short_name} is ready.`),unsaved?' Reloading loses the entries not yet saved.':''),reload,dismiss);
  document.body.append(bar);
}
window.addEventListener('online',()=>{if(me)void onPulse();});
// Offline: the live stream stops too (a late message must not say Connected); the 10-second check reopens it.
window.addEventListener('offline',()=>{stream?.close();stream=null;showConnection(false);});
// Draft edits save straight away; their toast offers Undo (see the undo registry in api.ts). "Undo last edit" does the same.
// The draft on screen names the revision (a stale one is refused: the conflict message says to reload).
const NO_UNDO='Nothing left to undo';
const draftOnScreen=async()=>scheduleCache.drafts.get(workspaceId)?.workspace||(await rotaMeta(workspaceId)).workspace;
async function undoLast(){
  const w=await draftOnScreen(),done=[{kind:'workspace.undo',result:await command('workspace.undo',{reason:'Undo'},w.id,w.revision).catch(e=>{throw undoRefused(e);})}];
  await applyUndone(done);notifyUndone(done,'Undone. The previous arrangement is back and checked again.');
}
// Its "Redone." message (the undo registry) offers Undo again.
async function redoLast(){await redoDraft(redoAfter?.id===workspaceId&&knownRevision(workspaceId)===redoAfter.revision?redoAfter:await draftOnScreen());}
const redoBarButton=()=>iconButton('Redo','redo',()=>redoLast(),{class:'bare','data-shortcut':'redo','data-tip':'Redo (Ctrl Shift Z)','aria-keyshortcuts':'Control+Shift+Z Meta+Shift+Z Control+Y'});
// An Undo the open page could not patch in place: only what the undone kinds can change is read (the session only for
// department, role, sign-in and other non-draft changes; the lists in memory only for non-draft changes), then this page is redrawn.
onUndone(async(done:Undone)=>{
  if(!me)return;restoreRulesViews(done);restoreFairnessViews(done);const restoredView=restoreTableViews(done);if(restoredView){applyPlannerView(restoredView.definition.planner,(restoredView.definition as PlannerSaved).conditions);setTableColumns('planner-list',restoredView.definition as PlannerSaved);plannerViewId=restoredView.id;if(plannerMemo)plannerMemo.views=[...plannerMemo.views.filter(v=>v.id!==restoredView.id),restoredView];}
  const kinds=done.map(d=>d.kind);readAfresh();for(const k of kinds)savedKinds.delete(k);
  // A non-draft undo moves the department's input revision: the session is read again with the lists, as refresh does,
  // so the live pulse that follows sees nothing new and does not redraw the page under the reader (an open cell editor).
  try{const lists=kinds.some(k=>!k.startsWith('workspace.'));
    if(lists||kinds.some(k=>/^(department|role|identity)\./.test(k))){me=await background<Me>('/api/me');applyDisplay();}
    if(lists)await loadCatalogues();
    // The planner reads its saved views afresh (a draw still finishing meanwhile kept the list from before the Undo).
    if(restoredView)plannerMemo=null;
    void loadNavBadges(true,kinds);await redraw();}
  catch(e){if(restoredView)plannerMemo=null;if(!sessionChanged(e))notify('The latest server state could not be retrieved. Previously confirmed saves remain saved. '+(e as Error).message,'error',undefined,e);}
});
let seenActivity:number|null=null;
/** The page's own saves whose pulses have not come yet, by the pulse value their replies named (several saves of what
 *  the value does not count, settings or rules, share one): each such pulse is already on screen (ownPulse in onPulse). */
let ownSaves={activity:NaN,n:0};
// Your own Undo's reply: Redo is offered on the rota it left, at that revision (any later change moves the revision),
// in the bar already on screen at once: the redraw waits for the draft to be read again.
window.addEventListener('rota-saved',e=>{const {kind,result}=(e as CustomEvent).detail||{},w=result?.workspace;
  if(!w?.id||plannerRota?.id!==w.id&&kind!=='workspace.undo')return;
  // Any other change to the rota on screen (a Redo too) takes Redo off at once.
  if(kind!=='workspace.undo'){document.querySelector('.planner-bar [data-shortcut="redo"]')?.remove();return;}
  redoAfter={id:String(w.id),revision:Number(w.revision)};
  const undo=plannerRota?.id===w.id?document.querySelector<HTMLButtonElement>('.planner-bar button[aria-label="Undo last edit"]'):null;
  if(undo&&w.can_undo===false)notYet(undo,NO_UNDO);
  if(undo&&!undo.parentElement!.querySelector('[data-shortcut="redo"]'))undo.after(redoBarButton());});
// The page's own change: its reply names the pulse value it leads to, so that pulse does not refresh a second time.
// The page's own edit (not a run: job.* commands drop the jobs read) keeps the last jobs read current, so the redraw
// after it does not read the jobs again (PL-03).
window.addEventListener('rota-saved',e=>{const {activity:a,kind}=(e as CustomEvent).detail||{};if(typeof a==='number'&&seenActivity!==null){
    if(lastJobs&&lastJobs.activity===seenActivity&&!String(kind).startsWith('job.'))lastJobs.activity=a;seenActivity=a;ownSaves=ownSaves.activity===a?{activity:a,n:ownSaves.n+1}:{activity:a,n:1};}
  if(String(kind).startsWith('notification.'))seenUnread=null;});  // marking read: the next badge count is read afresh
// Live updates: the server pushes the pulse when something changes (EventSource reconnects by itself). The periodic
// check stays as a fallback: every minute while the stream is alive, every 10 seconds when it is not.
let stream:EventSource|null=null,streamSeen=0,seenGeneration:number|undefined,streamFailures=0,streamResume=0;
function listen(){
  stream?.close();stream=null;if(!me||typeof EventSource==='undefined')return;
  const query=new URLSearchParams();for(const id of watchedJobs())query.append('job_id',id);
  const source=new EventSource('/api/events'+(query.size?'?'+query:''));stream=source;
  source.addEventListener('pulse',e=>{if(stream!==source)return;streamSeen=Date.now();streamFailures=0;const pulse=JSON.parse((e as MessageEvent).data);
    // A running auto-schedule reported progress (planners only): its panel reads the job at once (polling is the fallback).
    if(pulse.generation_progress!==undefined)seenGeneration=pulse.generation_progress;
    noteDriftInputs(pulse.changes,pulse.input_revision);
    // A job changed (or anything may have): a wait on a job (api.ts awaitJob) reads its status now.
    const jobs=(pulse.changes as LiveChange[]|null)?.filter(c=>c.scope==='jobs');
    if(pulse.changes===null||jobs?.length)window.dispatchEvent(new CustomEvent('jobs-changed',{detail:pulse.changes===null||jobs?.some(c=>!c.id)?null:jobs?.map(c=>c.id)}));
    // The rule catalogue changed (its projection may be ready): a read waiting on it (api.ts projectionReady) reads again.
    if(pulse.changes===null||(pulse.changes as LiveChange[]).some(c=>c.scope==='catalogue'))window.dispatchEvent(new Event('catalogue-changed'));
    if(pulse.changes?.length)queuePulse(pulse);else void onPulse(pulse);});
  source.addEventListener('ping',()=>{if(stream===source){streamSeen=Date.now();streamFailures=0;}});
  // The server's `error` (a failure it recovers from) or a connection the browser gave up on never signs out: only
  // `expired` below, or a 401 from /api/me or /api/pulse (api.ts), does. Reconnect after a bounded, jittered backoff
  // (1 s doubling to 60 s); the periodic check reads the pulse meanwhile. A connection the browser is retrying is left to it.
  source.addEventListener('error',e=>{if(stream!==source||!(e instanceof MessageEvent)&&source.readyState!==EventSource.CLOSED)return;
    source.close();stream=null;const wait=Math.min(60_000,1000*2**streamFailures++)*(0.5+Math.random()/2);streamResume=Date.now()+wait;
    window.setTimeout(()=>{if(me&&!stream&&document.visibilityState!=='hidden')listen();},wait);});
  source.addEventListener('expired',()=>{source.close();if(stream!==source)return;stream=null;window.dispatchEvent(new CustomEvent('session-expired'));});
}
window.addEventListener('job-subscriptions-changed',()=>{if(me)listen();});
window.addEventListener('session-expired',()=>{stream?.close();stream=null;watchPanelJob('');});
let lastPoll=0,deferredPulse:Row|null=null;
/** Two pulses as one: the newer state with both pulses' changes (either naming none: everything changed). */
// Bounded: past 64 distinct changes (a form open through a long import) it becomes one full refresh.
function mergePulse(older:Row|null,newer:Row):Row{
  if(!older||older===newer)return newer;
  const all=Array.isArray(older.changes)&&Array.isArray(newer.changes)?[...new Map(([...older.changes,...newer.changes] as LiveChange[]).map(c=>[c.scope+' '+(c.id??''),c])).values()]:null;
  // Collapse only the affected scopes when the bounded list fills; never widen it to every page.
  return {...newer,merged:true,changes:all&&all.length>64?[...new Set(all.map(c=>c.scope))].map(scope=>({scope,id:null})):all};
}
/** A live update kept for later (a form open, a save under way) keeps every change it holds. */
const deferPulse=(pulse:Row)=>{deferredPulse=mergePulse(deferredPulse,pulse);};
// A burst of pushed changes (an import, a bulk edit, several tabs saving) is applied once: pulses arriving within 300 ms,
// or while the last one is still being applied, are merged and applied together.
let burst:Row|null=null,burstTimer=0;
function queuePulse(pulse:Row){burst=mergePulse(burst,pulse);if(!burstTimer)burstTimer=window.setTimeout(flushPulse,300);}
function flushPulse(){burstTimer=0;if(polling||!burst)return;const pulse=burst;burst=null;void onPulse(pulse);}  // onPulse flushes when done
window.setInterval(()=>{
  if(!me||document.visibilityState==='hidden'||resuming())return;
  if(deferredPulse){void onPulse(deferredPulse);return;}
  if(me&&!stream&&Date.now()>=streamResume)listen();
  const live=Date.now()-streamSeen<45_000;
  // The Inbox follows the stream too (its pulse carries the unread count); the minute's check catches the rest.
  // Operations follows the worker every 10 s while it has work: an outbox's state changes are not pushed on the stream.
  if(live&&Date.now()-lastPoll<60_000&&!(view==='system'&&routeParts[0]==='operations'&&opsBusy))return;
  lastPoll=Date.now();void onPulse();
},10_000);
// The live stream carries the unread count: the bell follows it, and a new arrival shows a message in the app, or a
// desktop alert when the tab is in the background and alerts are on (Account menu).
let seenUnread:number|null=null;
/** The bell and the app icon say the server's unread total (unreadCount); the app icon's number is the bell's (D30). */
function showUnread(unread:number){if(unread)navBadges.inbox=unreadCount(unread);else delete navBadges.inbox;applyNavBadges();setBadge(unread);}
async function inboxPulse(unread:number){
  showUnread(unread);
  const rise=seenUnread!==null&&unread>seenUnread;seenUnread=unread;
  if(!rise)return;
  const latest=(await background<{notifications:Row[]}>('/api/notifications')).notifications.find(n=>!noteRead(n));
  if(!latest)return;
  if(document.hidden&&tabAlertsOn()){
    const options={body:String(latest.message||''),tag:'rota-'+latest.id,icon:'/api/pwa/apple-touch-icon.png'};
    const worker=await navigator.serviceWorker?.getRegistration().catch(()=>undefined);
    if(worker)await worker.showNotification(String(latest.title),options);else new Notification(String(latest.title),options);
  }else if(view!=='inbox')notify(String(latest.title),'info',{label:'Open inbox',run:()=>navigate('inbox')});
}
/** Someone is typing, or has changes not yet saved (a rule's draft lines): a live update is not drawn over them. */
// A field that cannot be edited (readonly, disabled) is not editing: focus merely left on it never holds live updates.
const editingNow=()=>!!document.activeElement?.matches('input:not([readonly]):not([disabled]),textarea:not([readonly]):not([disabled]),select:not([disabled]),[contenteditable="true"]')||!!leaveCheck?.()||dockDirty();
/** …nor over an open dialog or popover (rota health, score, a filter). */
// A timeline gesture (its ghost) is someone using the page too: a pulse never redraws the board under it.
// A command on its way too: a redraw would replace the panel or dialog that waits for its reply (an approval closing
// itself before the activation commits); the pulse waits and is replayed after it, once the page has refreshed itself.
const readerBusy=()=>editingNow()||savingNow()||!!document.querySelector('dialog[open],.popover,.tl-ghost');
/** Whether the page on screen shows what a live change touched (paths[view].live, .liveIds). */
function liveNeeds(c:LiveChange){
  const route=paths[view]||paths.home,ids=route.liveIds?.()[c.scope];
  return c.scope!=='jobs'&&route.live.includes(c.scope)&&(!c.id||!ids||ids.includes(c.id));
}
async function onPulse(given?:Row){
  if(!me||polling)return;polling=true;
  if(given&&given===deferredPulse)deferredPulse=null;  // replayed now; a branch below keeps it again if it must wait
  try{const pulse:Row=given||await background<Row>('/api/pulse');if(!given)noteDriftInputs(pulse.changes,pulse.input_revision);
    if(seenActivity!==null&&pulse.activity<seenActivity)return;
    noticeNewVersion(pulse.build);if(typeof pulse.unread==='number')void inboxPulse(pulse.unread);const key=JSON.stringify([pulse.input_revision,pulse.availability_revision,pulse.roles]);
    // Someone else moved a rota, swap or request: refresh once (the first poll only records the starting point).
    const moved=seenActivity!==null&&pulse.activity!==seenActivity;
    // A pulse that overtakes this page's own save waits for the save's reply (which names the value it leads to).
    if(moved&&savingNow()){deferPulse(pulse);return;}
    const opsWatch=view==='system'&&routeParts[0]==='operations'&&opsBusy;
    // The pulse names what changed (null: anything may have): only what the page shows is read again. An owed redraw, a
    // lost connection or a busy worker on Operations still refresh in full.
    // An empty list on a moved state is a change nobody announced (the stream's periodic re-check): anything may have.
    const now=Date.now();for(const [id,at] of checkSettled)if(now-at>=15000)checkSettled.delete(id);
    const rawChanges:LiveChange[]|null=Array.isArray(pulse.changes)&&!opsWatch?pulse.changes:null;
    // A worker completion names its validation job on the workspace change.
    // The waiting check consumes that job's compact delta; do not spend a full
    // workspace refresh before the settle path applies it.
    const {changes:others,skip:checkOnly}=withoutOwnCheck(rawChanges,c=>view==='schedule'&&c.scope==='workspace'&&c.id===workspaceId&&!!c.job_id&&
      (checkSettled.has(String(c.job_id))||(autoChecking&&(checkJob?.id===c.job_id||autoCheckKey.startsWith(String(c.id)+':')))),moved);
    // Another person's edit that carries its delta is drawn from memory (no read) when the held draft is at its base; a
    // reader busy with a form or popover keeps the whole pulse for later, as before (readerBusy, below).
    let drawn=false;
    const remote=others&&moved&&!savingNow()&&!readerBusy()?others.filter(c=>{const r=remoteEdit(c);return !!r&&applyRemoteEdit(r);}):[];
    const changes=remote.length?others!.filter(c=>!remote.includes(c)):others;
    // The next schedule view reads afresh; a pulse kept for later (a popover, dialog or field open) forgets when it is
    // applied, not now: the 10 s replay of a kept pulse wiped the cache and every filter change read the rota again (maintenance).
    const forget=moved&&(!changes||changes.some(c=>paths.schedule.live.includes(c.scope))),busy=readerBusy();let held=false;
    // So are the replies kept for opening pages (api.ts forgetReads: all of them, whatever this page shows); the
    // kept answers of each consumer by the scopes it names (naming none: all).
    const keptScopes=rawChanges?.length?new Set(others!.map(c=>c.scope)):null;
    const forgetting=()=>{applyDriftInputs();if(moved)forgetReads(keptScopes,keptScopes&&others);if(forget)forgetSchedule();};
    const hold=(p:Row)=>{held=true;deferPulse(p);};
    if(!busy)forgetting();
    // The page's own save's pulse (at the value the save's reply named, nothing since, not merged with another's): its
    // changes are drawn already, so it reads and redraws nothing (a redraw under the person rebuilt open menus and moved
    // focus, once the stream opened at boot). A worker's completion (job_id) is still read. Each save's pulse once.
    // shortcut: matched by value and order, not by command; an echoed command id on the change (backend) would be exact.
    const ownPulse=!moved&&!pulse.merged&&ownSaves.n>0&&pulse.activity===ownSaves.activity;if(ownPulse)ownSaves.n--;
    const needed=changes?.filter(c=>!(ownPulse&&!c.job_id)&&liveNeeds(c));
    const loadedKey=JSON.stringify([me.organisation.input_revision,me.organisation.availability_revision,[...me.roles].sort()]);
    const editing=editingNow();
    // F-L2: roles compared as sets, and never from a pulse without them (a restarting server): an unchanged account must
    // not rebuild the app and drop an open form. A real change waits while a form is being edited (the server refuses
    // whatever the new roles no longer permit), then rebuilds and says why.
    const rolesChanged=Array.isArray(pulse.roles)&&JSON.stringify([...me.roles].sort())!==JSON.stringify([...pulse.roles].sort());
    const stale=!!changes?.length||key!==loadedKey||moved||!connected||redrawOwed||opsWatch;
    // Revision-keyed caches (schedule windows, academic time) see the new revisions without reading the account.
    const adopt=()=>{me!.organisation.input_revision=pulse.input_revision;me!.organisation.availability_revision=pulse.availability_revision;};
    // Nothing on this page changed: no read at all, only the counts in the navigation those changes can move. An update
    // still waiting (a form open) takes these changes with it.
    if(checkOnly){adopt();seenActivity=pulse.activity;}
    else if(stale&&changes&&!rolesChanged&&!needed!.length){
      if(deferredPulse)hold(pulse);
      else{adopt();seenActivity=pulse.activity;const scopes=new Set(changes.map(c=>c.scope));
        // The Schedule's own edit: its check, once it lands, reads the counts (autoCheck settle), as the redraw did before.
        if(!(ownPulse&&view==='schedule'&&plannerMemo)&&Object.values(BADGE_SOURCES).some(b=>b.live.some(s=>scopes.has(s))))void loadNavBadges(true,[],scopes);}}
    // Any dialog holds it, open or not: the browser closes a dialog with unsaved changes by itself (a repeated Escape)
    // and showDialog opens it again a task later, so a restart in that instant would drop it unsaid. Every app dialog
    // is removed when it closes (showDialog, palette, the sign-in shade on resuming), so none holds it for ever.
    if(rolesChanged&&(readerBusy()||document.querySelector('dialog')))hold(pulse);
    else if(rolesChanged){deferredPulse=null;catalogues={people:[]};const was=me;await boot();if(me&&me!==was)notify('Your access changed.','info');}
    // Swaps and review routes redraw only when something moved (the activity counter covers swaps): an unconditional
    // redraw every 10 s took keyboard focus away. Operations follows the worker only while it has work (opsBusy);
    // imports run inside their command, so Imports redraws on a change like every other page.
    else if(stale&&(!needed||needed.length)){
      // Someone is typing or a form is open: keep the update and apply it when they are done.
      // An open popover (rota health, score, a filter) is not pulled from under the reader either.
      if(readerBusy())hold(pulse);
      else{deferredPulse=null;seenActivity=pulse.activity;if(needed)adopt();const account=sessionKey();drawn=true;await refresh(true,needed?new Set(needed.map(c=>c.scope)):undefined,needed);
        if(refreshSkipped&&sessionKey()===account)deferPulse(pulse);}}
    // A pushed pulse that moved neither activity nor the unread count changes nothing here; the periodic check (no
    // pulse given) still reads, since equal counts can hide a changed inbox (one read, another arrived).
    else if(view==='inbox'&&!changes&&(!given||JSON.stringify([pulse.activity,pulse.unread])!==inboxPulseKey)){
      if(editing||savingNow()||document.querySelector('dialog[open]'))hold(pulse);
      else{
        inboxPulseKey=JSON.stringify([pulse.activity,pulse.unread]);
        const serial=renderSerial,identity=me.identity_id;
        const latest=await background<NotificationList>(inboxPath());
        if(view!=='inbox'||serial!==renderSerial||me?.identity_id!==identity)return;
        deferredPulse=null;seenActivity=pulse.activity;
        // A stable inbox keeps its focus and scroll; equal unread counts can still contain different records.
        if(inboxSig(latest.notifications)!==inboxSnapshot){inboxPending=latest;await renderPage();}
      }
    }
    else if(seenActivity===null)seenActivity=pulse.activity;  // boot could not read the baseline: start from here
    if(busy&&!held)forgetting();
    // The remote edits applied above, with nothing else to read: the schedule is drawn again from memory.
    if(remote.length&&!drawn&&view==='schedule')void redraw();
    // Do not consume a changed pulse while a form is open: refresh after it closes.
    showConnection(true);
  }catch(e){if(sessionChanged(e))return;showConnection(false,e);}
  finally{polling=false;if(burst&&!burstTimer)flushPulse();}
}
// Installable app: the worker keeps only the app's own files for starting offline and shows push alerts. An alert's tap
// hands its address here: an in-app address goes through the same unsaved-changes guard as any link.
startWorker(target=>void (async()=>{const url=new URL(target,location.origin);if(url.origin!==location.origin)return;
  // /n/{id}: the server says where it leads now, after its own access checks; gone or private opens the inbox.
  const n=me?await alertNotification(url):undefined;
  if(n!==undefined){if(n)openNoteTarget(n as Row);else navigate('inbox');return;}
  if(url.pathname===location.pathname&&url.hash){if(location.hash!==url.hash)location.hash=url.hash;return;}
  if(leaving(()=>location.assign(url.href)))location.assign(url.href);})());
// Every dropdown in the app opens the one searchable picker (picker.ts).
enhanceSelects();
enhanceTooltips();
// One-time invitation and password-reset links open their own page, without signing in.
const accountLink=/^\/(?:invite|reset)\/([A-Za-z0-9_-]{20,100})$/.exec(location.pathname);
// Send feedback from anywhere: the reporter on error messages, Ctrl Shift F, and what a report calls a page.
installFeedback({me:()=>me,navigate,zone:()=>me?zone():'UTC',pageLabel:route=>{const r=parseRoute(route.replace(/^#?\/?/,'#'));const tab=PAGE_TABS[r.page]?.find(t=>t.key===r.parts[0]);
  return paths[r.page]?[paths[r.page].title,tab?.label].filter(Boolean).join(' › '):r.page||'Home';}});
// Start-up waits like any page (N-26): the moving line alone, then "Still loading · 0:15" after 15 s; a read that never
// answers ends (READ_TIMEOUT) in the start-up failure screen with Retry.
readBranding();if(accountLink)void accountLinkPage(root,accountLink[1]);else{
  // The static start-up screen (index.html) keeps its emblem and name; its line becomes the shared wait, shown at once
  // when the page has already waited past 300 ms for this module (no blink).
  const wait=skeleton('boot',`Connecting to ${brand().short_name}…`);if(performance.now()>SHOW_AFTER)wait.classList.remove('sk-wait');
  document.querySelector('#main.boot>.sk-boot')?.replaceWith(wait);void boot();}
