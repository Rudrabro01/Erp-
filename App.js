import { useState, useEffect } from 'react';
import '@/App.css';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { SettingsProvider, useSettings } from '@/contexts/SettingsContext';
import { Layout } from '@/components/Layout';
import { Toaster } from '@/components/ui/sonner';
import { GraduationCap } from 'lucide-react';
import api from '@/utils/api';

import LoginPage from '@/pages/LoginPage';
import Dashboard from '@/pages/Dashboard';
import AttendancePage from '@/pages/AttendancePage';
import StudentsPage from '@/pages/StudentsPage';
import StudentProfile from '@/pages/StudentProfile';
import MarksPage from '@/pages/MarksPage';
import ReportsPage from '@/pages/ReportsPage';
import AdminPanel from '@/pages/AdminPanel';

const FALLBACK_COLLEGE = 'Guru Gobind Singh Polytechnic';
const FALLBACK_DEPT = 'Nashik';
const FALLBACK_LOGO = '/logo.png';

// Splash Screen
const SplashScreen = ({ onDone, settings }) => {
  useEffect(() => {
    if (settings) {
      const timer = setTimeout(onDone, (settings.splash_duration || 3) * 1000);
      return () => clearTimeout(timer);
    }
  }, [settings, onDone]);

  const logoSrc = settings?.logo_url || FALLBACK_LOGO;

  return (
    <motion.div
      className="fixed inset-0 z-[100] splash-gradient flex flex-col items-center justify-center"
      exit={{ opacity: 0 }}
      transition={{ duration: 0.5 }}
    >
      <motion.div
        initial={{ scale: 0, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: 'spring', delay: 0.2 }}
        className="bg-white/10 backdrop-blur-md p-5 rounded-3xl shadow-2xl border border-white/20 inline-flex justify-center items-center mb-8"
      >
        <img
          src={logoSrc}
          alt="College Logo"
          className="w-20 h-20 object-contain"
          onError={e => { e.target.onerror = null; e.target.src = '/default-logo.png'; }}
        />
      </motion.div>
      <motion.h1
        initial={{ y: 30, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ delay: 0.5 }}
        className="text-3xl md:text-4xl font-bold text-white font-['Manrope'] text-center"
      >
        {settings?.college_name || FALLBACK_COLLEGE}
      </motion.h1>
      <motion.p
        initial={{ y: 20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ delay: 0.7 }}
        className="text-white/70 mt-3 text-base md:text-lg tracking-wide"
      >
        {settings?.department_name || FALLBACK_DEPT}
      </motion.p>
      <motion.div
        initial={{ width: 0 }}
        animate={{ width: 180 }}
        transition={{ delay: 1, duration: (settings?.splash_duration || 3) - 1, ease: 'linear' }}
        className="h-1 bg-white/40 rounded-full mt-10"
      />
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.2 }}
        className="text-white/40 text-xs mt-6 uppercase tracking-widest"
      >
        ERP System
      </motion.p>
    </motion.div>
  );
};

// Protected Route
const ProtectedRoute = ({ children, adminOnly }) => {
  const { user, loading } = useAuth();
  if (loading) return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="w-8 h-8 border-3 border-primary border-t-transparent rounded-full animate-spin" />
    </div>
  );
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && user.role !== 'admin') return <Navigate to="/dashboard" replace />;
  return <Layout>{children}</Layout>;
};

// Main Router
const AppRouter = () => {
  const { user, loading: authLoading } = useAuth();
  const { settings, loading: settingsLoading } = useSettings();
  const [showSplash, setShowSplash] = useState(true);

  if (authLoading || settingsLoading) return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="w-8 h-8 border-3 border-primary border-t-transparent rounded-full animate-spin" />
    </div>
  );

  return (
    <>
      <AnimatePresence>
        {showSplash && <SplashScreen onDone={() => setShowSplash(false)} settings={settings} />}
      </AnimatePresence>

      {!showSplash && (
        <Routes>
          <Route path="/login" element={user ? <Navigate to="/dashboard" replace /> : <LoginPage />} />
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
          <Route path="/attendance" element={<ProtectedRoute><AttendancePage /></ProtectedRoute>} />
          <Route path="/students" element={<ProtectedRoute><StudentsPage /></ProtectedRoute>} />
          <Route path="/students/:id" element={<ProtectedRoute><StudentProfile /></ProtectedRoute>} />
          <Route path="/marks" element={<ProtectedRoute><MarksPage /></ProtectedRoute>} />
          <Route path="/reports" element={<ProtectedRoute><ReportsPage /></ProtectedRoute>} />
          <Route path="/admin" element={<ProtectedRoute adminOnly><AdminPanel /></ProtectedRoute>} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      )}
    </>
  );
};

function App() {
  return (
    <ThemeProvider>
      <SettingsProvider>
        <AuthProvider>
          <BrowserRouter>
            <Toaster position="top-right" richColors closeButton />
            <AppRouter />
          </BrowserRouter>
        </AuthProvider>
      </SettingsProvider>
    </ThemeProvider>
  );
}

export default App;
