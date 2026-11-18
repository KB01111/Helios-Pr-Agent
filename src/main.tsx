import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";

// Not wrapped in <StrictMode>: boot effects start the daemon / SSE side effects once.
createRoot(document.getElementById("root") as HTMLElement).render(<App />);
