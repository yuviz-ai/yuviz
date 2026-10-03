// Every user-visible string in the Easy agent-creation flow. Easy renders text
// from here and never a server `detail`. Keep these plain-spoken: the Python
// test scans every string literal in this file for banned technical words.

import { ApiError } from "./api";

export const easyCopy = {
  stepTitles: [
    "What should my AI receptionist do?",
    "Tell it about my business",
    "Choose how it speaks",
    "Test it",
    "Fix anything that's wrong",
    "Put it to work",
  ],

  // A job needs something the account has none of: show this and link to where it is added.
  connectAiService: "Connect an AI service to continue",
  setUpSpeechRecognition: "Set up speech recognition to continue",
  setUpVoice: "Set up a voice to continue",

  // Step 3 dropdowns appear only when the account has more than one choice.
  voiceLabel: "Voice",
  aiServiceLabel: "AI service",
  speechRecognitionLabel: "Speech recognition",

  doubleBraces: "Please remove double curly brackets {{ }} from this text.",

  notFixable:
    "These instructions were changed by hand, so they can't be fixed automatically here. You can still edit them on the receptionist's page.",
  customerData:
    "That fix would copy details from a specific customer into the instructions. Describe the problem in general terms and try again.",
  customerDataExample: "For example: it asked for the caller's date of birth before checking who they were.",
  unusableOutput: "We couldn't turn that into a safe fix. Try describing the problem differently.",
  aiUnavailable: "The AI service didn't answer. Nothing was changed. Please try again.",
  aiCantRunThis: "Your AI service can't run this yet.",
  tooManyRequests: "You're going a little fast. Please wait a moment and try again.",
  testExpired: "This test has ended. Start a new one.",
  changedMeanwhile: "Something changed while you were working. Reload the page and try again.",
  notAllowed: "You don't have permission to do that.",
  notFound: "We couldn't find that. Reload the page and try again.",
  generic: "Something went wrong. Please try again.",

  // Step 1
  loading: "Loading...",
  advancedLink: "Advanced setup",
  chooseAccount: "Choose an account from the switcher at the top to continue.",
  channelLabels: { phone_in: "Answers calls", phone_out: "Makes calls", chat: "Chats" },
  whatItDoes: "What it does",
  whatItWontDo: "What it won't do",
  whenItHandsOff: "When it passes the call to a person",
  addItLink: "Add it",

  // Step 2
  nameLabel: "Name",
  businessNameLabel: "Business name",
  businessFactsLabel: "Business facts",
  businessFactsHint: "Hours, address, prices, anything callers often ask. Optional.",
  nameRequired: "Please enter a name.",
  businessNameRequired: "Please enter your business name.",
  factsTooLong: "Business facts can be at most 1,000 characters.",
  nameTaken: "You already have one with that name. Please pick a different name.",

  // Step 2: optional documents the receptionist can look up.
  documentsLabel: "Documents your receptionist can look up (optional)",
  documentsHint: "It searches these when a caller asks something.",
  collectionsLabel: "Collections you already have",
  uploadFilesLabel: "Upload files",
  uploadFilesHint: "Text (.txt) or Markdown (.md) files only.",
  filesChosenLabel: "Files to upload",
  removeFile: "Remove",
  wrongFileType: "Only .txt and .md files can be uploaded. Other files were skipped.",
  uploadNeedsSetup: "File uploads need document search set up first.",
  uploadNeedsSetupLink: "Set it up",
  documentsWarning: "Your receptionist was created, but some documents didn't get attached.",
  documentsTryAgain: "Try again",
  documentsRetrying: "Trying again...",
  documentsAttachedLabel: "Documents it can look up",

  // Step 3
  languageLabel: "Language",
  languageAutomatic: "Choose automatically",
  chooseOne: "Choose one",

  // Navigation
  cancel: "Cancel",
  back: "Back",
  continue: "Continue",
  creating: "Setting it up...",

  // Step 4
  startTalking: "Start talking",
  stop: "Stop",
  youSaid: "You",
  itSaid: "Your receptionist",
  statusIdle: "Ready when you are.",
  statusConnecting: "Connecting...",
  statusReady: "Listening. Go ahead and speak.",
  statusTalking: "Hearing you...",
  statusThinking: "Thinking...",
  statusSpeaking: "Speaking...",
  statusEnded: "The test has ended.",
  statusError: "The test could not start. Check your microphone and try again.",
  transcriptEmpty: "What you say and what it answers will show up here.",
  chatPlaceholder: "Type what a caller would say",
  send: "Send",
  startChatTest: "Start a test chat",
  startOver: "Start a new test",
  testHint: "Try it like a real caller would. When you are done, continue to fix anything that sounded wrong.",

  // Step 5
  testFirst: "Run a test first. Then you can tell us what went wrong and we will fix it.",
  problemLabel: "What went wrong?",
  problemPlaceholder: "For example: it kept asking the same question twice.",
  suggestFix: "Suggest a fix",
  working: "Working on it...",
  beforeLabel: "Before",
  afterLabel: "After",
  acceptFix: "Accept this fix",
  discardFix: "Discard",
  undoFix: "Undo last change",
  fixAccepted: "The fix is saved. Test it again to check it.",
  fixUndone: "The last change was undone.",
  acceptRefused: "That change can't be saved. Try describing the problem differently.",

  // Step 6
  putToWork: "Put it to work",
  activating: "Turning it on...",
  liveTitle: "It's on.",
  liveNoteCalls: "It won't answer calls until a phone number is assigned to it.",
  liveNoteChat: "It won't answer chats until a chat channel is assigned to it.",
  assignNumberLink: "Assign a phone number",
  assignChatLink: "Assign a chat channel",
  passedOnCallsLink: "Choose who takes passed-on calls",
} as const;

export function easyErrorText(err: unknown): string {
  if (!(err instanceof ApiError)) return easyCopy.generic;
  const { status, detail } = err;
  if (detail === "customer_data") return easyCopy.customerData;
  if (detail === "unusable_output") return easyCopy.unusableOutput;
  if (detail === "prompt_not_fixable") return easyCopy.notFixable;
  if (detail === "ai_unavailable" || status === 502) return easyCopy.aiUnavailable;
  if (detail.includes("double curly brackets")) return easyCopy.doubleBraces;
  if (status === 429) return easyCopy.tooManyRequests;
  if (status === 404 && detail === "test session not found") return easyCopy.testExpired;
  if (status === 404) return easyCopy.notFound;
  if (status === 409) return easyCopy.changedMeanwhile;
  if (status === 403) return easyCopy.notAllowed;
  return easyCopy.generic;
}
