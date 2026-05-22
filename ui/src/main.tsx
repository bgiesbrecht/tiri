import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ToastProvider } from "./hooks/use-toast";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <TooltipProvider delayDuration={250}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </TooltipProvider>
  </React.StrictMode>,
);
