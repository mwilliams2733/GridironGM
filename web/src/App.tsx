import { Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Dashboard } from "@/pages/Dashboard";
import { Draft } from "@/pages/Draft";
import { Waivers } from "@/pages/Waivers";
import { Lineup } from "@/pages/Lineup";
import { NotFound } from "@/pages/NotFound";

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/draft" element={<Draft />} />
        <Route path="/waivers" element={<Waivers />} />
        <Route path="/lineup" element={<Lineup />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}

export default App;
