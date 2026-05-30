import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import { MeProvider } from './me.jsx';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <MeProvider>
      <App />
    </MeProvider>
  </React.StrictMode>
);
