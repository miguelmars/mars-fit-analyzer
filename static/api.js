const api = {
  async coach(){try{return await fetch(API+'/gpt/adaptive-coach').then(r=>r.json())}catch{return{error:true,message:'Coach no disponible'}}},
  async wellness(){try{return await fetch(API+'/gpt/wellness-summary').then(r=>r.json())}catch{return{error:true}}},
  async yearlySummary(){try{return await fetch(API+'/api/strava/yearly-summary').then(r=>r.json())}catch{return{error:true}}},
  async recentWeeks(n=12){try{return await fetch(API+'/api/strava/recent-weeks?weeks='+n).then(r=>r.json())}catch{return{error:true}}},
  async dashboard(){try{return await fetch(API+'/gpt/dashboard').then(r=>r.json())}catch{return{error:true}}},
  async marsContext(){try{return await fetch(API+'/gpt/mars-context').then(r=>r.json())}catch{return{error:true}}},
  async goals(){try{return await fetch(API+'/gpt/goals').then(r=>r.json())}catch{return{error:true}}},
};
