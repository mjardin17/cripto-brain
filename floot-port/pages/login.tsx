import { useState } from "react";
import { Activity, ArrowLeft, LockKeyhole, ShieldCheck } from "lucide-react";
import { PasswordLoginForm } from "../components/PasswordLoginForm";
import { PasswordRegisterForm } from "../components/PasswordRegisterForm";
import styles from "./login.module.css";

export default function LoginPage() {
  const [register, setRegister] = useState(false);
  return <main className={styles.page}>
    <div className={styles.shell}>
      <a className={styles.back} href="/"><ArrowLeft size={14}/> Back to Command Center</a>
      <section className={styles.card}>
        <div className={styles.brand}><span><Activity size={21}/></span><div><b>The Certainty Stack</b><small>SECURE RESEARCH TERMINAL</small></div></div>
        <div className={styles.rule}/>
        <div className={styles.intro}><div className={styles.icon}><LockKeyhole size={18}/></div><h1>{register ? "Create workspace account" : "Owner sign in"}</h1><p>{register ? "Create an account to access the research workspace." : "Sign in to access your private research workspace."}</p></div>
        {register ? <PasswordRegisterForm/> : <PasswordLoginForm/>}
        <button className={styles.switch} onClick={() => setRegister(!register)}>{register ? "Already have an account? Log in" : "Need to create an account? Register"}</button>
        <div className={styles.security}><ShieldCheck size={14}/><span>Session-protected access · Role-based permissions · Live trading disabled</span></div>
      </section>
      <footer>THE CERTAINTY STACK <span>·</span> AUTHENTICATION & ACCESS CONTROL</footer>
    </div>
  </main>;
}