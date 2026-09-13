import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import App from "./App";
import { Home } from "./Home";
import { Login } from "./Login";
import { Projects } from "./Projects";
import { Scenes } from "./Scenes";
import { DepartmentInbox } from "./DepartmentInbox";
import { DropCapture } from "./DropCapture";
import { RequireAccount } from "./RequireAccount";
import "./theme.css";
import "./styles.css";
import "./home.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        {/* Always the home page, signed in or out: opening the console and
            signing in are both actions taken from here, not the default
            landing spot for either state. */}
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<Login />} />
        <Route
          path="/projects"
          element={
            <RequireAccount>
              <Projects />
            </RequireAccount>
          }
        />
        <Route
          path="/scenes"
          element={
            <RequireAccount>
              <Scenes />
            </RequireAccount>
          }
        />
        <Route path="/dashboard" element={<App />} />
        <Route path="/inbox/:department" element={<DepartmentInbox />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <DropCapture />
    </BrowserRouter>
  </StrictMode>,
);
