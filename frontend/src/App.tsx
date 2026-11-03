import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import ConfigurePage from "./pages/ConfigurePage";
import LivePage from "./pages/LivePage";
import InsightsPage from "./pages/InsightsPage";
import { WizardProvider } from "./wizard/WizardContext";

export default function App() {
  return (
    <WizardProvider>
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/configure" replace />} />
        <Route path="/configure" element={<ConfigurePage />} />
        <Route path="/live" element={<LivePage />} />
        <Route path="/insights" element={<InsightsPage />} />
        <Route path="*" element={<Navigate to="/configure" replace />} />
      </Route>
    </Routes>
    </WizardProvider>
  );
}
