import { stopStream } from "./actions";
import { useGh } from "./github";
import { useApp } from "./store";
import { toastOk } from "./toast";

/** Wipe every session secret, stop the live stream and reset the GitHub session. */
export function forgetSecrets() {
  stopStream();
  useGh.getState().reset();
  useApp.getState().wipeSecrets();
  toastOk("Secrets cleared", "Session values and stream tokens wiped.");
}
