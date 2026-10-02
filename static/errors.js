/* Only messages intended for people may reach the interface. */
(function(){
 window.jailUserError=function(message){var error=new Error();error.userMessage=message;return error;};
 window.jailErrorMessage=function(error,fallback){
  if(error&&error.userMessage)return error.userMessage;
  if(error&&error.name==='AbortError')return 'Сервер долго не отвечает. Проверьте связь и попробуйте ещё раз.';
  if(error instanceof TypeError)return 'Нет связи с сервером. Проверьте интернет. Введённые значения остаются на экране.';
  return fallback||'Не удалось получить подтверждение. Обновите страницу и проверьте результат перед повтором.';
 };
 window.jailReadResponse=async function(response){
  if(response.redirected)throw jailUserError('Страница входа изменилась. Откройте сайт заново и выберите свою роль.');
  const messages={400:'Не удалось обработать введённые данные. Проверьте поля и попробуйте ещё раз.',403:'Это действие недоступно вашей роли.',404:'Эта запись больше не найдена. Обновите список.',405:'Не удалось выполнить действие. Обновите страницу и попробуйте ещё раз.',413:'Файл слишком большой. Выберите файл меньшего размера.',429:'Слишком много действий подряд. Подождите немного и повторите.',503:'Система сейчас занята. Подождите немного и попробуйте ещё раз.'};
  if(response.status>=500)throw jailUserError(messages[response.status]||'Сервер временно не может выполнить действие. Попробуйте немного позже.');
  var result;try{result=JSON.parse(await response.text());}catch(_){throw jailUserError(messages[response.status]||'Не удалось получить подтверждение от сервера. Обновите страницу и проверьте результат перед повтором.');}
  if(!result||typeof result!=='object'||Array.isArray(result)||typeof result.ok!=='boolean')throw jailUserError('Не удалось получить подтверждение от сервера. Обновите страницу и проверьте результат.');
  if(!response.ok&&response.status!==400&&response.status!==409)throw jailUserError(messages[response.status]||'Не удалось выполнить действие. Обновите страницу и попробуйте ещё раз.');
  if(!response.ok&&!result.error)throw jailUserError(messages[response.status]||'Не удалось выполнить действие. Обновите страницу и попробуйте ещё раз.');
  return result;
 };
})();
